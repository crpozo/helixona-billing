"""
Helixona Billing Agent — Monitoring Dashboard
Real-time web UI to see what the agent is doing.
"""
import json
import re
from decimal import Decimal
import subprocess
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request, send_file, redirect
import boto3
import os
from src.aws.clients import scan_all
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

session = boto3.Session(
    aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
    region_name=os.environ.get('AWS_REGION', 'us-west-2')
)

dynamodb = session.resource('dynamodb')
sqs = session.client('sqs')
SQS_URL = os.environ['SQS_QUEUE_URL']
SQS_URL_RESUB = os.environ.get('SQS_QUEUE_URL_RESUB', '')
SQS_URL_EOB = os.environ.get('SQS_QUEUE_URL_EOB', '')
S3_BUCKET = os.environ.get('S3_BUCKET_NAME', '')
EC2_IP = "54.189.175.233"
KEY_FILE = "infra/helixona-agent-key.pem"

# Per-bot routing: which SQS queue + systemd unit each tab targets.
# Each bot is its own systemd unit with its own X display, noVNC port, SQS
# queue and Chrome profile. Keep this table in step with QUEUE_BY_ROLE in
# src/aws/clients.py — the agent side of the same mapping.
# Each bot also has a name that says what it does, in the vocabulary of a
# clinic visit: Intake takes a claim into the payer for the first time,
# Follow-up returns to one already on file, Remittance brings back what the
# payer remits — payments and the explanation of benefits. One place to
# change them.
BOT_ROUTING = {
    'submissions': {
        'queue_url': SQS_URL,
        'service': 'helixona-agent',
        'name': 'Intake',
        'emoji': '📋',
        'label': 'Blue Shield Submissions',
        'novnc_port': 6080,
    },
    'resubmissions': {
        'queue_url': SQS_URL_RESUB,
        'service': 'helixona-agent-resub',
        'name': 'Follow-up',
        'emoji': '🩺',
        'label': 'Blue Shield Resubmissions',
        'novnc_port': 6081,
    },
    'eob': {
        'queue_url': SQS_URL_EOB,
        'service': 'helixona-agent-eob',
        'name': 'Remittance',
        'emoji': '🧾',
        'label': 'Check reconciliation',
        'novnc_port': 6083,
    },
}


def _bot_from_request():
    """Resolve which bot a request targets. Defaults to 'submissions'."""
    bot = (request.args.get('bot') or '').strip()
    if not bot:
        body = request.get_json(silent=True) or {}
        bot = (body.get('bot') or '').strip()
    if bot not in BOT_ROUTING:
        bot = 'submissions'
    return bot

# ── Internal 16-state mapping (used for DynamoDB) ──
STATE_LABELS = {
    1: "Claims ECW (Ready to Submit)", 2: "HCFA Generated",
    3: "Awaiting BS Claim #", 4: "BS Claim # Captured",
    5: "Medical Record Pulled", 6: "AI Verification Complete",
    7: "Cover Letter Generated", 8: "SympliSend Started",
    9: "FLN# Captured", 10: "Paid", 11: "Reduced / Denied",
    12: "Appeal in Progress", 13: "Closed",
    14: "Under Review", 15: "Resubmitted", 16: "Final"
}

# ── Simplified pipeline stages for the UI ──
# Maps internal states → display stage
PIPELINE_STAGES = {
    "documentation": {"label": "Documentation Completed", "color": "#CDB486", "states": [1, 2, 3, 4, 5, 6, 7]},
    "ready":         {"label": "Ready to Submit to SympliSend", "color": "#E8D5B0", "states": [8]},
    "submitted":     {"label": "Submitted to SympliSend", "color": "#b09968", "states": [9, 10, 13, 16]},
    "revision":      {"label": "Revision Required SympliSend", "color": "#8a7450", "states": [11, 12, 14, 15]},
}

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Helixona Billing Agent — Dashboard</title>
<link rel="icon" type="image/png" href="/static/helixona-logo.png">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;500;600;700&family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#07070a;--bg2:#0c0c12;--panel:#0f0f16;--panel2:#15151e;--card:#14141c;--card2:#1a1a24;
  --bdr:#1f1f2b;--bdr2:#2a2a38;
  --accent:#CDB486;--accent2:#b09968;--accent-glow:rgba(205,180,134,.18);
  --vio:#8b5cf6;--vio2:#a78bfa;--vio3:#c4b5fd;--viod:#6d28d9;
  --success:#10b981;--warning:#f59e0b;--bad:#ef4444;--info:#3b82f6;
  --text-primary:#f1f5f9;--text-secondary:#94a3b8;--text-muted:#64748b;--text-dim:#475569;
  --border:#1f1f2b;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{background:var(--bg);color:var(--text-primary);font-family:'Inter',sans-serif;min-height:100vh;font-size:13px;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
input,textarea,select,button{font-family:'Inter',sans-serif}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bdr2);border-radius:3px}

/* ========== LAYOUT ========== */
.app{display:block;min-height:100vh}
.shell{padding:18px 26px 40px;min-width:0}

/* ========== SIDEBAR ========== */
.brand{display:flex;align-items:center;gap:10px;padding:0 14px 0 0;margin-right:4px;border-right:1px solid var(--bdr)}
.brand img{height:30px;opacity:.95}
.brand-t{font-family:'Playfair Display',serif;font-size:15px;font-weight:600;color:var(--accent);line-height:1}
.brand-s{font-size:9px;color:var(--text-muted);text-transform:uppercase;letter-spacing:1.2px;margin-top:3px}


.sb-sec{font-size:9px;text-transform:uppercase;letter-spacing:1.5px;color:var(--text-dim);font-weight:600;padding:0 10px;margin-top:8px;display:flex;align-items:center;gap:6px}
.sb-list{display:flex;flex-direction:column;gap:3px;max-height:240px;overflow-y:auto}
.sb-item{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:8px;cursor:pointer;transition:all .15s}
.sb-item:hover{background:var(--card)}
.sb-ico{width:26px;height:26px;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0;font-weight:600}
.sb-info{min-width:0;flex:1}
.sb-l{font-size:9px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.6px}
.sb-n{font-size:11px;color:var(--text-primary);font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

.sb-foot{margin-top:auto;background:linear-gradient(135deg,rgba(239,68,68,.1),transparent);border:1px solid rgba(239,68,68,.22);border-radius:11px;padding:12px;text-align:center}
.sb-foot-t{font-size:11px;font-weight:600;color:var(--bad);margin-bottom:2px}
.sb-foot-s{font-size:9px;color:var(--text-muted)}

/* ========== TOPBAR ========== */
.tb{display:flex;align-items:center;justify-content:space-between;margin-bottom:22px;gap:14px;flex-wrap:wrap}
.tb-l{display:flex;align-items:center;gap:14px}
.acct{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--bdr);padding:6px 14px 6px 6px;border-radius:30px}
.avatar{width:32px;height:32px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--viod));display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;color:#000}
.acct-i{line-height:1.2}
.acct-h{font-size:10px;color:var(--text-muted);display:flex;align-items:center;gap:6px}
.acct-h .pro{background:var(--accent);color:#000;font-size:8px;font-weight:700;padding:1px 5px;border-radius:4px;letter-spacing:.5px}
.acct-n{font-size:12px;font-weight:600;color:var(--text-primary)}

.tb-r{display:flex;align-items:center;gap:10px}
.icbtn{width:36px;height:36px;border-radius:9px;background:var(--card);border:1px solid var(--bdr);display:flex;align-items:center;justify-content:center;cursor:pointer;color:var(--text-secondary);position:relative;transition:all .2s;font-size:14px}
.icbtn:hover{color:var(--text-primary);border-color:var(--bdr2)}

.btn{padding:9px 16px;border-radius:9px;font-size:12px;font-weight:600;cursor:pointer;border:none;transition:all .2s;display:inline-flex;align-items:center;gap:7px;color:var(--text-primary);background:transparent}
.btn:hover{background:var(--card)}
.btn-refresh{background:transparent;border:1px solid var(--bdr);color:var(--text-secondary);padding:5px 10px;font-size:11px}
.btn-refresh:hover{border-color:var(--accent);color:var(--accent)}
.btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#000;width:100%;padding:11px;justify-content:center}
.btn-primary:hover{box-shadow:0 6px 20px rgba(205,180,134,.3)}

.stop-btn{padding:8px 16px;border-radius:9px;font-size:12px;font-weight:600;cursor:pointer;color:var(--bad);border:1px solid rgba(239,68,68,.35);background:rgba(239,68,68,.08);transition:all .2s;display:inline-flex;align-items:center;gap:6px}
.stop-btn:hover{background:rgba(239,68,68,.18);box-shadow:0 6px 20px rgba(239,68,68,.2)}
.stop-btn.stopping{opacity:.6;cursor:wait}

.novnc-link{display:flex;align-items:center;gap:6px;padding:8px 14px;border-radius:9px;font-size:11px;color:var(--text-secondary);background:var(--card);border:1px solid var(--bdr);transition:all .2s}
.novnc-link:hover{border-color:var(--accent);color:var(--accent)}
.live-screen{margin:0 24px 14px;border:1px solid var(--bdr);border-radius:12px;background:var(--card);overflow:hidden}
.live-screen-bar{display:flex;align-items:center;gap:14px;padding:8px 14px;font-size:12px;border-bottom:1px solid var(--bdr)}
.live-screen-bar a{color:var(--accent);font-size:11px;margin-left:auto}
.live-screen iframe{display:block;width:100%;height:min(72vh,820px);border:0;background:#000}

.status-badge{display:flex;align-items:center;gap:7px;background:rgba(16,185,129,.08);border:1px solid rgba(16,185,129,.25);padding:6px 12px;border-radius:30px;font-size:11px;color:var(--success);font-weight:500}
.status-dot{width:6px;height:6px;background:var(--success);border-radius:50%;animation:p 2s infinite}
@keyframes p{0%,100%{box-shadow:0 0 0 0 rgba(16,185,129,.4)}50%{box-shadow:0 0 0 5px transparent}}

/* Row currently being processed by the agent */
tr.processing-row{background:rgba(59,130,246,.10) !important;animation:rowPulse 1.6s ease-in-out infinite}
@keyframes rowPulse{0%,100%{box-shadow:inset 3px 0 0 var(--info)}50%{box-shadow:inset 3px 0 0 #93c5fd}}
.proc-badge{display:inline-flex;align-items:center;gap:6px;background:rgba(59,130,246,.15);border:1px solid rgba(59,130,246,.4);color:var(--info);padding:3px 9px;border-radius:30px;font-size:10px;font-weight:600;white-space:nowrap}
.proc-spin{width:8px;height:8px;border-radius:50%;background:var(--info);box-shadow:0 0 0 0 rgba(59,130,246,.5);animation:p 1.2s infinite}

/* ========== HERO ========== */
.hero{display:grid;grid-template-columns:1fr 1fr 1fr 340px;gap:14px;margin-bottom:18px}
.hero-h{display:flex;align-items:center;justify-content:space-between;grid-column:1/-1;margin-bottom:-2px}
.hero-h-l{display:flex;align-items:center;gap:12px}
.hero-h-t{font-family:'Playfair Display',serif;font-size:28px;font-weight:600;letter-spacing:-.5px}
.hero-h-l .rec{display:flex;align-items:center;gap:6px;color:var(--text-muted);font-size:11px}
.pill-ct{background:var(--card);border:1px solid var(--bdr);padding:4px 10px;border-radius:30px;font-size:10px;color:var(--text-secondary);font-weight:600}
.chip{background:var(--card);border:1px solid var(--bdr);padding:6px 12px;border-radius:8px;font-size:11px;font-weight:500;color:var(--text-secondary);cursor:pointer;display:flex;align-items:center;gap:6px;transition:all .15s}
.chip:hover{color:var(--text-primary);border-color:var(--bdr2)}
.chip .ar{font-size:8px;color:var(--text-muted)}

.bigcard{background:linear-gradient(180deg,var(--panel) 0%,var(--bg2) 100%);border:1px solid var(--bdr);border-radius:16px;padding:18px;position:relative;overflow:hidden;display:flex;flex-direction:column;min-height:200px;transition:all .25s}
.bigcard:hover{border-color:var(--bdr2);transform:translateY(-2px);box-shadow:0 10px 30px rgba(0,0,0,.4)}
.bc-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px}
.bc-id{display:flex;align-items:center;gap:9px}
.bc-icon{width:30px;height:30px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:14px}
.bc-meta{line-height:1.25}
.bc-tag{font-size:9px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.7px;margin-bottom:2px}
.bc-name{font-size:13px;font-weight:600;color:var(--text-primary)}
.bc-arrow{width:28px;height:28px;border-radius:8px;background:var(--card);border:1px solid var(--bdr);display:flex;align-items:center;justify-content:center;color:var(--text-secondary);font-size:11px;cursor:pointer;transition:all .15s}
.bc-arrow:hover{color:var(--text-primary);transform:rotate(-45deg)}
.bc-lbl{font-size:10px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px}
.bc-num{font-family:'Playfair Display',serif;font-size:36px;font-weight:700;line-height:1.05;letter-spacing:-1px}
.bc-num .pct{font-size:20px;color:var(--text-secondary);font-weight:500;margin-left:2px}
.bc-delta{display:inline-flex;align-items:center;gap:5px;background:rgba(16,185,129,.1);color:var(--success);font-size:11px;font-weight:600;padding:3px 9px;border-radius:30px;margin-top:8px;width:fit-content}
.bc-delta.dn{background:rgba(239,68,68,.1);color:var(--bad)}
.bc-spark{position:absolute;bottom:0;left:0;right:0;height:70px;pointer-events:none}

.promo{background:radial-gradient(120% 100% at 100% 0%,rgba(167,139,250,.28),transparent 60%),linear-gradient(160deg,#1e1531 0%,#0d0a18 100%);border:1px solid rgba(167,139,250,.25);border-radius:16px;padding:20px;position:relative;overflow:hidden;display:flex;flex-direction:column;justify-content:space-between;min-height:200px}
.promo::before{content:'';position:absolute;top:-30px;right:-30px;width:160px;height:160px;background:radial-gradient(circle,rgba(167,139,250,.18),transparent 70%);pointer-events:none}
.promo-top{display:flex;justify-content:space-between;align-items:flex-start;position:relative}
.promo-logo{display:flex;align-items:center;gap:7px;font-family:'Playfair Display',serif;font-size:13px;color:#fff;font-weight:600}
.promo-logo .d{width:14px;height:14px;border-radius:50%;background:linear-gradient(135deg,#fff,#c4b5fd)}
.promo-new{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.18);font-size:9px;color:#fff;padding:3px 8px;border-radius:30px;font-weight:600;letter-spacing:.6px}
.promo-h{font-family:'Playfair Display',serif;font-size:20px;font-weight:600;color:#fff;line-height:1.15;margin-top:10px;position:relative}
.promo-p{font-size:11px;color:rgba(255,255,255,.65);line-height:1.5;margin-top:8px;position:relative}
.promo-btns{display:flex;flex-direction:column;gap:8px;margin-top:14px;position:relative}
.promo-btn{background:rgba(255,255,255,.92);color:#1a0b3a;padding:10px;border-radius:9px;font-weight:600;font-size:12px;text-align:center;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:7px;transition:all .2s;border:none}
.promo-btn:hover{background:#fff;transform:translateY(-1px)}
.promo-btn.alt{background:rgba(255,255,255,.08);color:#fff;border:1px solid rgba(255,255,255,.15)}
.promo-btn.alt:hover{background:rgba(255,255,255,.14)}

/* ========== HERO KPI (submission progress) ========== */
.hero-kpi{background:linear-gradient(135deg,var(--panel) 0%,var(--bg2) 100%);border:1px solid var(--bdr);border-radius:16px;padding:24px 26px;margin-bottom:18px;position:relative;overflow:hidden}
.hero-kpi::after{content:'';position:absolute;top:-50px;right:-50px;width:240px;height:240px;background:radial-gradient(circle,var(--accent-glow),transparent 60%);pointer-events:none}
.hero-kpi-top{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:16px;flex-wrap:wrap;gap:14px;position:relative}
.hero-kpi-headline{font-family:'Playfair Display',serif;line-height:1}
.hero-num{font-size:68px;font-weight:700;color:var(--accent);letter-spacing:-2.5px;text-shadow:0 0 28px var(--accent-glow)}
.hero-denom{font-size:18px;color:var(--text-muted);font-family:'Inter';font-weight:500;margin-left:8px}
.hero-denom #hero-total{color:var(--text-primary);font-weight:600}
.hero-kpi-pct{font-size:13px;color:var(--text-secondary);text-align:right}
.hero-kpi-pct strong{font-family:'Playfair Display',serif;font-size:22px;color:var(--success);font-weight:600;margin-right:4px}
.hero-progress{position:relative;height:8px;background:var(--card2);border-radius:4px;overflow:hidden}
.hero-progress-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--success));border-radius:4px;transition:width .5s ease;box-shadow:0 0 18px var(--accent-glow)}
.date-filter{display:flex;align-items:center;gap:8px;margin:0 0 14px;position:relative;flex-wrap:wrap}
.date-filter label{font-size:11px;color:var(--text-muted);font-weight:600;text-transform:uppercase;letter-spacing:.4px}
.date-filter select,.date-filter input[type=date]{background:var(--card2);border:1px solid var(--bdr2);border-radius:8px;color:var(--text-primary);font-size:12px;padding:6px 9px;font-family:'Inter';color-scheme:dark}
.date-filter input[type=date]:focus,.date-filter select:focus{outline:none;border-color:var(--accent)}
.date-filter .df-clear{background:none;border:1px solid var(--bdr2);border-radius:8px;color:var(--text-muted);font-size:12px;padding:6px 10px;cursor:pointer;display:none}
.date-filter .df-clear:hover{color:var(--text-primary);border-color:var(--accent)}
.date-filter.active .df-clear{display:inline-block}
.date-filter .df-hint{font-size:11px;color:var(--accent);font-weight:600;display:none}
.date-filter.active .df-hint{display:inline}

/* ========== PIPELINE ========== */
.pipeline-section{margin-bottom:20px}
.section-title{font-size:13px;font-weight:600;margin:0 0 12px;display:flex;align-items:center;gap:10px;color:var(--text-primary)}
.pipeline-flow{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.pipeline-stage{background:linear-gradient(180deg,var(--panel) 0%,var(--bg2) 100%);border:1px solid var(--bdr);border-radius:14px;padding:18px 16px 22px;position:relative;cursor:pointer;transition:all .25s;overflow:hidden}
.pipeline-stage:hover{border-color:var(--bdr2);transform:translateY(-2px);box-shadow:0 10px 24px rgba(0,0,0,.4)}
.stage-count{font-family:'Playfair Display',serif;font-size:42px;font-weight:700;line-height:1;letter-spacing:-1.5px;margin-bottom:6px}
.stage-name{font-size:11px;color:var(--text-secondary);font-weight:500;line-height:1.35;margin-bottom:12px}
.stage-bar{position:absolute;bottom:0;left:0;right:0;height:3px;box-shadow:0 0 16px currentColor}
.stage-details{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px}
.detail-chip{background:var(--card);border:1px solid var(--bdr);font-size:9.5px;padding:3px 7px;border-radius:30px;color:var(--text-secondary);font-weight:500}
.detail-chip.has-count{color:var(--text-primary)}
.stage-arrow{position:absolute;right:-12px;top:50%;transform:translateY(-50%);width:22px;height:22px;background:var(--card);border:1px solid var(--bdr);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;color:var(--text-secondary);z-index:3}

/* ========== SUB-TABS & MAIN GRID ========== */
.subtabs{display:flex;gap:18px;border-bottom:1px solid var(--bdr);padding-bottom:0;margin-bottom:16px;overflow-x:auto}
.subtab{padding:10px 0;font-size:12px;font-weight:600;color:var(--text-muted);cursor:pointer;border-bottom:2px solid transparent;transition:all .15s;white-space:nowrap;display:flex;align-items:center;gap:6px}
.subtab .s{font-size:9px;color:var(--text-dim);font-weight:500;display:block}
.subtab.on{color:var(--text-primary);border-bottom-color:var(--accent)}

.main{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:18px;align-items:start}

/* ========== CLAIMS TABLE ========== */
.claims-section{background:var(--panel);border:1px solid var(--bdr);border-radius:14px;overflow:hidden}
.claims-section .section-title{padding:14px 18px;margin:0;border-bottom:1px solid var(--bdr)}
.mfa-box{background:var(--card2);border:1px solid var(--bdr2);border-radius:10px;padding:10px 12px;margin:0 0 14px}
.mfa-box label{display:block;font-size:11px;font-weight:600;margin-bottom:6px}
.mfa-box input{background:var(--card);border:1px solid var(--bdr2);border-radius:8px;color:var(--text-primary);font-size:16px;letter-spacing:4px;padding:6px 10px;width:150px;font-family:'Inter'}
.mfa-box input:focus{outline:none;border-color:var(--accent)}
.claims-search{background:var(--card2);border:1px solid var(--bdr2);border-radius:8px;color:var(--text-primary);font-size:12px;padding:6px 10px;font-family:'Inter';width:300px;margin-left:14px}
.claims-search:focus{outline:none;border-color:var(--accent)}
.claims-search::placeholder{color:var(--text-dim)}
.claims-table-wrap{overflow-x:auto;max-height:760px;overflow-y:auto}
.claims-table-wrap.hide-payer .col-payer{display:none}
/* One main-column panel per tab — the claims table on Intake and Follow-up,
   the checks table on Remittance — pinned so an extra panel can never again
   push the rail out of its column. */
.main > #checks-section, .main > #folders-section, .main > #claims-section-submissions{grid-column:1}
.ftree details{margin:2px 0 2px 14px;border-left:1px solid var(--bdr);padding-left:10px}
.ftree > details{margin-left:0;border-left:none;padding-left:0}
.ftree summary{cursor:pointer;padding:6px 4px;list-style:none;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.ftree summary::-webkit-details-marker{display:none}
.ftree summary::before{content:'▸';display:inline-block;width:12px;color:var(--text-muted);transition:transform .15s}
.ftree details[open] > summary::before{transform:rotate(90deg)}
.ftree .fname{font-weight:600}
.ftree .fcount{font-size:11px;color:var(--text-muted)}
.ftree .fcount b{color:var(--text-secondary);font-weight:600}
.ftree table{width:auto;min-width:60%;margin:4px 0 8px 26px;font-size:12px}
.ftree td{padding:5px 10px;border-bottom:1px solid var(--bdr);vertical-align:top}
.ftree td.num{text-align:right;font-variant-numeric:tabular-nums}
.ftree .unread{color:var(--warning)}
.ftree mark{background:rgba(205,180,134,.35);color:inherit;border-radius:3px;padding:0 2px}
.chk-filter{font-size:11px;padding:3px 9px;border:1px solid var(--bdr);border-radius:12px;background:transparent;color:inherit;cursor:pointer}
.chk-filter.on{border-color:var(--accent);color:var(--accent)}
.btn.on{border-color:var(--accent);color:var(--accent)}
.chk-tiles{display:flex;flex-wrap:wrap;gap:10px;padding:10px 14px 4px}
.chk-tile{min-width:118px;padding:10px 12px;border:1px solid var(--bdr);border-radius:10px;background:var(--card);cursor:pointer}
.chk-tile-n{font-size:22px;font-weight:700;line-height:1.1}
.chk-tile-l{font-size:11px;color:var(--text-muted);margin-top:2px}
.chk-tile.on{background:var(--card2)}
.chk-tile-m{font-size:10px;color:var(--text-dim);margin-top:3px}
.verdict-pill{display:inline-block;padding:3px 9px;border-radius:30px;font-size:10.5px;font-weight:600;white-space:nowrap;background:var(--card2)}
.todo{font-size:11px;line-height:1.5;color:var(--text-secondary);max-width:280px}
.todo strong{color:var(--text-primary)}
.action{background:var(--card);border:1px solid var(--bdr);border-radius:10px;padding:12px 14px;margin-bottom:10px}
.action-t{font-size:12.5px;font-weight:600;color:var(--text-primary);display:flex;align-items:center;gap:8px}
.action-d{font-size:11px;color:var(--text-secondary);line-height:1.5;margin:5px 0 8px}
.action-row{display:flex;gap:8px;align-items:center}
.action-row input{margin:0 !important;flex:1}
.action .btn-run{background:var(--accent);color:#111;border:none;border-radius:8px;padding:8px 14px;font-weight:700;font-size:11.5px;cursor:pointer;white-space:nowrap}
.action .btn-run:hover{background:var(--accent2)}
.advanced{margin-top:12px;border-top:1px solid var(--bdr);padding-top:10px}
.advanced summary{font-size:11px;color:var(--text-muted);cursor:pointer;list-style:none}
.advanced summary::before{content:'▸ ';color:var(--text-dim)}
.advanced[open] summary::before{content:'▾ '}
.advanced[open] summary{margin-bottom:10px}
.main > .task-panel{grid-column:2;grid-row:1}
.eob-plan{padding:10px 12px 14px;border-top:1px solid var(--bdr)}
.eob-plan .btn-plan{font-size:12px;padding:5px 10px;border:1px solid var(--bdr);border-radius:6px;background:transparent;color:inherit;cursor:pointer}
.eob-plan .plan-hint{font-size:11px;color:var(--text-muted);margin-left:8px}
.plan-head{margin:10px 0 6px;font-size:12px}
.plan-ok{color:var(--success);font-weight:600}
.plan-held{color:var(--warning);font-weight:600}
.plan-tot{color:var(--text-muted);margin-left:8px}
.plan-reasons{margin:4px 0 8px 18px;padding:0;font-size:12px}
/* A reason wraps inside the card instead of stretching the table's
   horizontal scroll out of reach. */
.plan-reasons li,.plan-claim-h,.plan-note,.plan-err{max-width:900px;overflow-wrap:anywhere}
.plan-claim{margin-top:10px;padding-top:8px;border-top:1px dashed var(--bdr)}
.plan-claim-h{font-size:12px;margin-bottom:4px}
.plan-note{font-size:11px;color:var(--text-muted);margin-top:4px}
.plan-err{color:var(--bad);font-size:12px;margin-top:8px}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table{width:100%;border-collapse:collapse}
thead th{background:rgba(255,255,255,.015);padding:11px 14px;text-align:left;font-size:9px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.6px;border-bottom:1px solid var(--bdr);position:sticky;top:0;z-index:2}
tbody td{padding:12px 14px;font-size:12px;border-bottom:1px solid var(--bdr);color:var(--text-secondary);vertical-align:top;line-height:1.45}
td.nowrap,th.nowrap{white-space:nowrap}
td .sub{font-size:10.5px;color:var(--text-muted);font-weight:400;margin-top:3px;white-space:nowrap}
.docs{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center}
.docs .doc{white-space:nowrap;display:inline-flex;align-items:center;gap:6px}
.docs .doc a{margin-left:0 !important}
.doc-missing{color:var(--text-dim);font-size:11px}
.docs-cell{min-width:230px}
#submitted-toggle{white-space:nowrap}
tbody tr{transition:background .15s}
tbody tr:hover{background:var(--card)}
tbody tr:last-child td{border-bottom:none}
.state-pill{display:inline-block;padding:3px 9px;border-radius:30px;font-size:10px;font-weight:600;white-space:nowrap}
.state-pill.submitted{background:rgba(16,185,129,.12);color:var(--success)}
.state-pill.documentation{background:rgba(205,180,134,.12);color:var(--accent)}
.state-pill.ready{background:rgba(59,130,246,.12);color:var(--info)}
.state-pill.revision{background:rgba(239,68,68,.12);color:var(--bad)}
.state-pill.pending{background:rgba(245,158,11,.12);color:var(--warning)}
.hcfa-link{cursor:pointer;color:var(--accent);font-size:11px;font-weight:500;padding:3px 7px;border-radius:6px;background:var(--accent-glow);display:inline-block;transition:all .15s}
.hcfa-link:hover{background:rgba(205,180,134,.25);box-shadow:0 0 0 1px var(--accent)}
.empty-state{text-align:center;padding:30px 20px;color:var(--text-muted);font-size:12px}

/* ========== TASK PANEL ========== */
.task-panel{display:flex;flex-direction:column;gap:14px}
.task-card{background:var(--panel);border:1px solid var(--bdr);border-radius:14px;padding:16px 18px}
.task-card h3{font-size:12px;font-weight:600;margin-bottom:12px;display:flex;align-items:center;gap:8px;color:var(--text-primary)}
.task-card label{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.7px;color:var(--text-muted);display:block;margin-bottom:6px;margin-top:8px}
.task-card label:first-of-type{margin-top:0}
.task-card select,.task-card textarea,.task-card input{width:100%;background:var(--bg2);border:1px solid var(--bdr);color:var(--text-primary);padding:9px 11px;border-radius:8px;font-size:12px;outline:none;font-family:'Inter',sans-serif;margin-bottom:6px}
.task-card select:focus,.task-card textarea:focus,.task-card input:focus{border-color:var(--accent)}
.task-card textarea{min-height:90px;font-family:'JetBrains Mono','Monaco',monospace;font-size:11px;line-height:1.5;resize:vertical}
.task-steps{background:var(--card);border:1px solid var(--bdr);border-radius:9px;padding:11px 14px;margin:10px 0 12px;font-size:11px;line-height:1.55}
.task-steps-title{font-size:11px;font-weight:600;color:var(--accent);margin-bottom:5px}
.task-steps-desc{color:var(--text-secondary);margin-bottom:6px}
.task-steps ol{margin:8px 0 0 18px;color:var(--text-secondary);font-size:10.5px;line-height:1.6}
.task-steps li{margin-bottom:4px}
.task-steps code{background:rgba(205,180,134,.1);color:var(--accent);padding:1px 5px;border-radius:4px;font-size:10px;font-family:'JetBrains Mono','Monaco',monospace}
.task-steps strong{color:var(--text-primary)}

/* logs */
.logs-panel{background:var(--panel);border:1px solid var(--bdr);border-radius:14px;display:flex;flex-direction:column;max-height:340px}
.logs-header{padding:12px 16px;border-bottom:1px solid var(--bdr);display:flex;justify-content:space-between;align-items:center}
.logs-header h3{font-size:12px;font-weight:600;display:flex;align-items:center;gap:6px}
.logs-body{flex:1;overflow-y:auto;padding:8px 14px;font-family:'JetBrains Mono','Monaco',monospace;font-size:10.5px;line-height:1.6}
.log-line{padding:2px 0;color:var(--text-secondary);word-break:break-all}
.log-line.info{color:var(--text-secondary)}
.log-line.success{color:var(--success)}
.log-line.warning{color:var(--warning)}
.log-line.error{color:var(--bad)}

/* fix coding ivs files */

/* toast */
.toast{position:fixed;bottom:30px;right:30px;background:var(--card);border:1px solid var(--bdr);padding:13px 22px;border-radius:11px;font-size:12px;font-weight:500;color:var(--text-primary);opacity:0;transform:translateY(20px);transition:all .3s;pointer-events:none;z-index:1000;box-shadow:0 10px 30px rgba(0,0,0,.5)}
.toast.success,.toast.error{opacity:1;transform:translateY(0)}
.toast.success{border-color:var(--success);box-shadow:0 10px 30px rgba(16,185,129,.3)}
.toast.error{border-color:var(--bad);box-shadow:0 10px 30px rgba(239,68,68,.3)}

/* HCFA Lightbox */
.hcfa-lightbox{position:fixed;inset:0;background:rgba(0,0,0,.92);backdrop-filter:blur(10px);z-index:1100;display:none;flex-direction:column;padding:24px}
.hcfa-lightbox.active{display:flex}
.hcfa-lightbox-header{display:flex;align-items:center;gap:18px;padding-bottom:14px;border-bottom:1px solid var(--bdr);flex-wrap:wrap}
.hcfa-lightbox-header h3{font-size:14px;font-weight:600;color:var(--text-primary)}
.hcfa-tabs{display:flex;gap:8px;flex:1}
.hcfa-tab{background:var(--card);border:1px solid var(--bdr);color:var(--text-secondary);padding:7px 14px;border-radius:8px;font-size:11px;font-weight:600;cursor:pointer;transition:all .15s}
.hcfa-tab:hover{color:var(--text-primary)}
.hcfa-tab.active{background:var(--accent);color:#000;border-color:var(--accent)}
.hcfa-lightbox-close{background:var(--card);border:1px solid var(--bdr);color:var(--text-secondary);width:34px;height:34px;border-radius:8px;cursor:pointer;font-size:14px;transition:all .15s}
.hcfa-lightbox-close:hover{color:var(--bad);border-color:var(--bad)}
.hcfa-lightbox-body{flex:1;overflow:auto;padding:18px;display:flex;align-items:center;justify-content:center}
.hcfa-lightbox-body img{max-width:100%;max-height:100%;object-fit:contain;border-radius:8px;box-shadow:0 0 40px rgba(0,0,0,.6)}
.hcfa-placeholder{text-align:center;color:var(--text-muted);font-size:13px;padding:40px}

/* BS Drawer */
.bs-drawer-overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);backdrop-filter:blur(4px);z-index:1090;opacity:0;pointer-events:none;transition:opacity .2s}
.bs-drawer-overlay.active{opacity:1;pointer-events:auto}
.bs-drawer{position:fixed;top:0;right:0;bottom:0;width:480px;max-width:96vw;background:var(--panel);border-left:1px solid var(--bdr);z-index:1095;transform:translateX(100%);transition:transform .3s ease;display:flex;flex-direction:column;box-shadow:-20px 0 40px rgba(0,0,0,.5)}
.bs-drawer.active{transform:translateX(0)}
.bs-drawer-header{padding:18px 22px;border-bottom:1px solid var(--bdr);display:flex;justify-content:space-between;align-items:center}
.bs-drawer-header h2{font-family:'Playfair Display',serif;font-size:18px;font-weight:600;color:var(--accent)}
.bs-drawer-close{background:var(--card);border:1px solid var(--bdr);color:var(--text-secondary);width:32px;height:32px;border-radius:8px;cursor:pointer;font-size:13px;transition:all .15s}
.bs-drawer-close:hover{color:var(--bad);border-color:var(--bad)}
.bs-drawer-meta{padding:12px 22px;border-bottom:1px solid var(--bdr);font-size:11px;color:var(--text-secondary)}
.bs-drawer-meta strong{color:var(--accent)}
.bs-drawer-body{flex:1;overflow-y:auto;padding:14px 22px}
.bs-claim-card{background:var(--card);border:1px solid var(--bdr);border-radius:10px;padding:12px 14px;margin-bottom:8px;transition:all .15s}
.bs-claim-card:hover{border-color:var(--accent)}

.footer{text-align:center;padding:22px 0 10px;color:var(--text-dim);font-size:10px;border-top:1px solid var(--bdr);margin-top:24px}
.footer a{color:var(--accent)}

@media(max-width:1320px){
  .hero{grid-template-columns:1fr 1fr 1fr}
  .hero .promo{grid-column:1/-1;min-height:auto;flex-direction:row;align-items:center;gap:20px}
  .hero .promo>div:nth-child(2){flex:1}
  .hero .promo-btns{flex-direction:row}
  .main{grid-template-columns:1fr}
  .main > .task-panel{grid-column:1;grid-row:auto}
}
@media(max-width:1080px){
  .pipeline-flow{grid-template-columns:repeat(2,1fr)}
  .hero{grid-template-columns:1fr 1fr}
}
@media(max-width:780px){
  .hero{grid-template-columns:1fr}
  .hero .promo{flex-direction:column}
  .hero-num{font-size:46px}
}
</style>
</head>
<body>
<div class="app">

  <!-- ═══════════════ MAIN ═══════════════ -->
  <div class="shell">

    <!-- TOPBAR -->
    <div class="tb">
      <div class="tb-l">
        <div class="brand">
          <img src="/static/helixona-logo.png" alt="Helixona">
          <div>
            <div class="brand-t">Helixona<sup style="font-size:8px;color:var(--text-muted)">®</sup></div>
            <div class="brand-s">Billing Agent · v1</div>
          </div>
        </div>
        <button id="stopBtn" class="stop-btn" onclick="stopAgent()">⛔ Stop Agent</button>
        <a id="novnc-link" href="http://54.189.175.233:6080/vnc.html" target="_blank" class="novnc-link">🖥️ noVNC</a>
        <button id="live-toggle" class="novnc-link" onclick="toggleLiveScreen()" title="Watch the active bot's browser here">👁️ Live screen</button>
      </div>
      <div class="tb-r">
        <div class="status-badge"><div class="status-dot"></div>Agent Running · 54.189.175.233</div>
        <span style="font-size:10px;color:var(--text-muted)" id="ts">—</span>
        <a href="/audit" class="novnc-link" title="Every submission the bot made, with its documents">🧾 Audit Log</a>
        <div class="icbtn" onclick="loadData();loadLogs()" title="Refresh">↻</div>
      </div>
    </div>

    <!-- BOT TABS -->
    <div class="subtabs" id="bot-tabs">
      <div class="subtab on" data-bot="submissions" onclick="setActiveBot('submissions')">📋 Intake · Blue Shield Submissions</div>
      <div class="subtab" data-bot="resubmissions" onclick="setActiveBot('resubmissions')">🩺 Follow-up · Blue Shield Resubmissions</div>
      <div class="subtab" data-bot="eob" onclick="setActiveBot('eob')">🧾 Remittance · Checks</div>
    </div>

    <!-- HERO KPI: Submission progress (Bot 1 — Submissions) -->
    <div class="hero-kpi" id="hero-submissions">
      <div class="hero-kpi-top">
        <div class="hero-kpi-headline">
          <span class="hero-num" id="hero-submitted">—</span>
          <span class="hero-denom"><span id="hero-num-label" hidden></span> of <span id="hero-total">—</span> <span id="hero-denom-label">claims submitted</span></span>
        </div>
        <div class="hero-kpi-pct"><strong id="hero-pct">—</strong> <span id="hero-pct-label">complete</span> · <span id="hero-remaining">—</span> <span id="hero-remaining-label">remaining</span></div>
      </div>
      <div class="date-filter" id="date-filter">
        <label for="df-from">Dates</label>
        <select id="df-field" onchange="onDateFilterChange()">
          <option value="dos">Service date</option>
          <option value="sent">Sent date</option>
        </select>
        <input type="date" id="df-from" onchange="onDateFilterChange()">
        <span style="color:var(--text-dim)">→</span>
        <input type="date" id="df-to" onchange="onDateFilterChange()">
        <button class="df-clear" onclick="clearDateFilter()">✕ Clear</button>
        <span class="df-hint" id="df-hint"></span>
      </div>
      <div class="hero-progress"><div class="hero-progress-fill" id="hero-progress-fill"></div></div>
    </div>


    <!-- LIVE SCREEN — the active bot's noVNC display, embedded -->
    <div class="live-screen" id="live-screen" hidden>
      <div class="live-screen-bar">
        <span id="live-screen-title">🖥️ Live screen</span>
        <a id="live-screen-open" href="#" target="_blank">open in a new tab ↗</a>
        <button class="btn" onclick="toggleLiveScreen()">✕ Hide</button>
      </div>
      <iframe id="live-screen-frame" title="Bot screen (noVNC)"></iframe>
    </div>

    <!-- MAIN: claims + admin rail -->
    <div class="main">

      <!-- CHEQUES (Remittance) — posted / unposted / not in eCW / no copy -->
      <div class="claims-section" id="checks-section" hidden>
        <div class="section-title">
          ✅ Checks
          <span id="checks-meta" style="font-size:11px;color:var(--text-muted);margin-left:8px;font-weight:500"></span>
          <a class="btn" href="/checks" target="_blank" style="margin-left:auto" title="The same checks on one page for the billing team">↗ Team page</a>
          <a class="btn" href="/api/checks.csv">⬇ CSV</a>
          <button class="btn btn-refresh" onclick="loadChecks()">↻ Refresh</button>
        </div>
        <div id="checks-run" style="font-size:12px;color:var(--text-muted);margin:8px 14px 0;line-height:1.7"></div>
        <div class="chk-tiles" id="checks-tiles"></div>
        <div class="claims-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Check #</th><th>Copy in SharePoint</th><th class="num">Amount</th><th>Blue Shield</th><th>eCW</th><th>Verdict</th><th>What to do</th>
              </tr>
            </thead>
            <tbody id="checks-body"><tr><td colspan="7" class="empty-state">No reconciliation yet. Use <strong>▶ Run → Reconcile all checks</strong> (or <strong>Test one check</strong>) on the right. If the log stops at Blue Shield’s 2-step, type the e-mailed code in the <strong>Blue Shield code</strong> box.</td></tr></tbody>
          </table>
        </div>
      </div>

      <!-- SHAREPOINT FOLDERS (Remittance) — what was collected from each folder, to check the reading -->
      <div class="claims-section" id="folders-section" hidden>
        <div class="section-title" style="cursor:pointer" onclick="toggleFolders()" title="What the bot read in each SharePoint folder — to check it is reading the right files">
          📁 SharePoint folders, as the bot read them
          <span id="folders-meta" style="font-size:11px;color:var(--text-muted);margin-left:8px;font-weight:500"></span>
          <button class="btn" id="folders-toggle" style="margin-left:auto" onclick="event.stopPropagation();toggleFolders()">Show</button>
        </div>
        <div id="folders-body" hidden>
          <div style="display:flex;gap:8px;align-items:center;padding:10px 14px 0">
            <input id="folders-search" class="claims-search" autocomplete="off" placeholder="Find a check # or file…" style="max-width:260px" oninput="renderFolders()">
            <button class="btn" onclick="foldersOpenAll(true)">Expand all</button>
            <button class="btn" onclick="foldersOpenAll(false)">Collapse</button>
            <button class="btn btn-refresh" onclick="loadFolders()">↻ Refresh</button>
          </div>
          <div class="ftree" id="folders-tree" style="padding:8px 14px 14px"><div class="empty-state">No folder has been read yet.</div></div>
        </div>
      </div>

      <!-- CLAIMS TABLE -->
      <div class="claims-section" id="claims-section-submissions">
        <div class="section-title">
          📋 Claims<span id="claims-payer-title" style="font-weight:500;color:var(--text-muted);margin-left:8px;font-size:12px;"></span>
          <span id="claims-type-counts" style="font-weight:500;margin-left:12px;font-size:11px;"></span>
          <input id="claims-search" class="claims-search" autocomplete="off"
                 placeholder="Search: patient, claim #, BS ref, DOS, subscriber…"
                 oninput="onClaimsSearch()">
          <button class="btn" id="submitted-toggle" onclick="toggleSubmitted()" title="Claims already sent to SympliSend are out of the way by default">Show submitted</button>
          <button class="btn btn-refresh" onclick="loadData()">↻ Refresh</button>
          <span id="claims-eta" style="font-size:11px;color:var(--info);font-weight:600;margin-left:auto;display:none">⏱ </span>
          <span id="claims-meta" style="font-size:11px;color:var(--text-muted);margin-left:14px"></span>
        </div>
        <div class="claims-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Claim #</th><th>Patient</th><th>DOS</th><th class="col-payer">Payer</th><th class="num">Charges</th>
                <th>Documents</th><th>Submission</th><th>Stage</th>
              </tr>
            </thead>
            <tbody id="claims-body"><tr><td colspan="8" class="empty-state">No claims yet. Use <strong>▶ Run → Get documentation from eCW</strong> on the right.</td></tr></tbody>
          </table>
        </div>
      </div>


      <!-- RIGHT RAIL -->
      <div class="task-panel">

        <!-- RUN: one card per thing the bot can do, in plain words -->
        <div class="task-card" id="send-task-card">
          <h3>▶ Run</h3>
          <div id="actions"></div>
          <div class="mfa-box" id="mfa-box">
            <label for="mfa-code">🔐 Blue Shield code <span style="font-weight:400;color:var(--text-muted)">— only when the log says the bot is waiting at Blue Shield's 2-step</span></label>
            <div style="display:flex;gap:8px;align-items:center">
              <input id="mfa-code" inputmode="numeric" maxlength="8" placeholder="6 digits" autocomplete="one-time-code"
                     onkeydown="if(event.key==='Enter'){event.preventDefault();sendMfaCode();}">
              <button class="btn" type="button" onclick="sendMfaCode()">Send code</button>
              <span id="mfa-status" style="font-size:11px;color:var(--text-muted)"></span>
            </div>
          </div>
          <details class="advanced" id="advanced">
            <summary>Advanced · raw task, delete data</summary>
            <label>Task Type</label>
            <select id="task-type" onchange="updateTaskTemplate()">
              <optgroup label="🛡️ Blue Shield Claims Bot" data-bot="submissions resubmissions">
                <option value="bs_missing_docs" data-bot="submissions resubmissions">📋 ECW Obtain Claims Documentation</option>
                <option value="blueshield_submissions" data-bot="submissions resubmissions">📤 Blue Shield Submissions</option>
                <option value="ecw_status_update" data-bot="submissions resubmissions">📝 ECW Status Update</option>
              </optgroup>
              <optgroup label="🧾 Remittance · EOB Bot" data-bot="eob">
                <option value="check_reconcile" data-bot="eob">✅ Reconcile checks: copies · Blue Shield · eCW</option>
                <option value="check_test_one" data-bot="eob">🧪 Test of 1: one Blue Shield check vs SharePoint vs eCW</option>
              </optgroup>
            </select>

            <div id="task-steps" class="task-steps"></div>

            <div style="margin:10px 0;padding:10px 12px;background:rgba(205,180,134,0.06);border:1px solid rgba(205,180,134,0.18);border-radius:8px">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
                <span style="color:var(--accent);font-weight:600;font-size:11px">🧪 Only these claims</span>
                <span style="color:var(--text-muted);font-size:10px">(empty = all · several: 239, 240, 241)</span>
              </div>
              <input type="text" id="test-claim-id" placeholder="e.g. 239 or 239, 240, 241" style="margin-bottom:0">
            </div>

            <label>Task Payload (JSON)</label>
            <textarea id="task-payload"></textarea>
            <button class="btn btn-primary" onclick="sendTask()">🚀 Send to SQS</button>
            <button class="btn" style="background:rgba(239,68,68,.1);color:var(--bad);border:1px solid rgba(239,68,68,.3);margin-top:8px;width:100%;padding:10px;border-radius:8px;font-weight:600;font-size:11.5px;cursor:pointer" onclick="deleteAllClaims()">🗑️ Delete All Claims &amp; Start Over</button>
          </details>
        </div>

        <!-- LIVE LOGS -->
        <div class="logs-panel">
          <div class="logs-header">
            <h3>🔴 Live Agent Logs</h3>
            <button class="btn" onclick="clearLogs('cleared by you')" title="Clear the panel; the journal on the server keeps everything">🗑 Clear</button>
            <button class="btn btn-refresh" onclick="loadLogs()">↻</button>
          </div>
          <div class="logs-body" id="logs-body">
            <div class="log-line info">Loading logs...</div>
          </div>
        </div>

        <!-- OPEN TASKS -->
        <div class="task-card" id="tasks-card">
          <h3>🚨 Open Tasks</h3>
          <div id="tasks-list">
            <div class="empty-state" style="padding:18px">No open tasks</div>
          </div>
        </div>

      </div>
    </div>

    <div class="footer">Helixona Billing Agent · v1 · <a href="https://helixona.com" target="_blank">helixona.com</a> · Auto-refresh 10s</div>

  </div>
</div>

<div class="toast" id="toast"></div>

<!-- HCFA Lightbox Viewer -->
<div class="hcfa-lightbox" id="hcfa-lightbox">
  <div class="hcfa-lightbox-header">
    <h3 id="hcfa-lightbox-title">HCFA Form</h3>
    <div class="hcfa-tabs" id="hcfa-tabs"></div>
    <button class="hcfa-lightbox-close" onclick="closeHCFAViewer()">✕</button>
  </div>
  <div class="hcfa-lightbox-body" id="hcfa-lightbox-body">
    <div class="hcfa-placeholder">Loading...</div>
  </div>
</div>

<!-- BS Claims Drawer -->
<div class="bs-drawer-overlay" id="bs-overlay" onclick="closeBSDrawer()"></div>
<div class="bs-drawer" id="bs-drawer">
  <div class="bs-drawer-header">
    <h2 id="bs-drawer-title">Claims</h2>
    <button class="bs-drawer-close" onclick="closeBSDrawer()">✕</button>
  </div>
  <div class="bs-drawer-meta" id="bs-drawer-meta">
    <span>Loading...</span>
  </div>
  <div class="bs-drawer-body" id="bs-drawer-body">
    <div class="empty-state">No claims data yet.</div>
  </div>
</div>

<script>
// ═══════════════ Augmentations on top of the original dashboard JS ═══════════════
// Wraps loadData to also render hero cards, sidebar list,
// nav counts and refreshed timestamp.
window.scrollToEl = function(sel){
  const el = document.querySelector(sel);
  if(el) el.scrollIntoView({behavior:'smooth',block:'start'});
};

(function(){
  const $ = id => document.getElementById(id);
  const getState = c => parseInt(c.state || c.current_state || 0);
  function getStages(){
    try { return PIPELINE_STAGES; } catch(e){ return {}; }
  }
  function stageKeyFor(s){
    for(const [k,v] of Object.entries(getStages()))
      if(v.states && v.states.includes(s)) return k;
    return 'documentation';
  }

  function areaSpark(values,color){
    const w=320,h=70,n=values.length||7;
    const mx=Math.max(...values,1), mn=Math.min(...values,0);
    const rng=mx-mn||1;
    const pts=values.map((v,i)=>{
      const x=(i/(n-1))*w;
      const y=h-((v-mn)/rng)*(h-16)-8;
      return [x,y];
    });
    const path=pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+','+p[1].toFixed(1)).join(' ');
    const area=path+` L${w},${h} L0,${h} Z`;
    const last=pts[pts.length-1];
    const gid='gs'+Math.random().toString(36).slice(2,8);
    return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:100%">
      <defs><linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0%" stop-color="${color}" stop-opacity=".35"/>
        <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
      </linearGradient></defs>
      <path d="${area}" fill="url(#${gid})"/>
      <path d="${path}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="filter:drop-shadow(0 0 4px ${color})"/>
      <circle cx="${last[0]}" cy="${last[1]}" r="3" fill="${color}" style="filter:drop-shadow(0 0 6px ${color})"/>
    </svg>`;
  }

  function renderHeroCards(claims){
    const stages = getStages() || {};
    const order = ['submitted','documentation','ready'];
    const iconBg = {
      submitted:'rgba(16,185,129,.15)',
      documentation:'rgba(205,180,134,.15)',
      ready:'rgba(232,213,176,.15)'
    };
    const icons = {submitted:'✅', documentation:'📄', ready:'🚀'};
    const wrap = $('hero-cards');
    if(!wrap) return;
    const t = claims.length;
    wrap.innerHTML = order.map(k=>{
      const st = stages[k];
      if(!st) return '';
      const n = claims.filter(c=>st.states.includes(getState(c))).length;
      const pct = t ? Math.round(n/t*100) : 0;
      const base = Math.max(1, Math.round(n*.6));
      const series = [base, base+1, base-1, base+2, base, base+1, n];
      const up = n >= base;
      const colors = {submitted:'#10b981', documentation:'#CDB486', ready:'#E8D5B0', revision:'#ef4444'};
      const c = colors[k] || '#CDB486';
      return `
        <div class="bigcard">
          <div class="bc-top">
            <div class="bc-id">
              <div class="bc-icon" style="background:${iconBg[k]};color:${c}">${icons[k]||'•'}</div>
              <div class="bc-meta"><div class="bc-tag">${k==='submitted'?'In Blue Shield':k==='ready'?'Docs complete':'Gathering docs'}</div><div class="bc-name">${st.label}</div></div>
            </div>
            <div class="bc-arrow">↗</div>
          </div>
          <div class="bc-lbl">Claim Count</div>
          <div class="bc-num" style="color:${c}">${n}<span class="pct">/${t||0}</span></div>
          <div class="bc-delta ${up?'':'dn'}"><span>${up?'▲':'▼'}</span>${pct}%</div>
          <div class="bc-spark">${areaSpark(series,c)}</div>
        </div>`;
    }).join('');
  }

  function renderSidebar(claims){
    const stages = getStages() || {};
    const colors = {submitted:'#10b981', documentation:'#CDB486', ready:'#E8D5B0', revision:'#ef4444'};
    const list = $('sb-list');
    if($('sb-cnt')) $('sb-cnt').textContent = claims.length;
    if($('nv-cl'))  $('nv-cl').textContent  = claims.length;
    if($('nv-pipe'))$('nv-pipe').textContent = Object.values(stages).reduce((a,st)=>a+claims.filter(c=>st.states.includes(getState(c))).length,0);
    if($('top-cnt'))$('top-cnt').textContent = (claims.length||0)+' Active';
    if(!list) return;
    if(!claims.length){
      list.innerHTML = '<div class="empty-state" style="padding:14px;font-size:10px">No claims</div>';
      return;
    }
    const sorted = [...claims].sort((a,b)=>getState(b)-getState(a)).slice(0,6);
    list.innerHTML = sorted.map(c=>{
      const sk = stageKeyFor(getState(c));
      const stg = stages[sk];
      const c0 = colors[sk] || '#CDB486';
      const initial = (c.patient_name || '?').charAt(0).toUpperCase();
      return `<div class="sb-item" title="${c.claim_id||''}">
        <div class="sb-ico" style="background:${c0}22;color:${c0}">${initial}</div>
        <div class="sb-info">
          <div class="sb-l">${(stg&&stg.label)||sk}</div>
          <div class="sb-n">${c.charges || c.claim_id || '—'}</div>
        </div>
      </div>`;
    }).join('');
  }

  function updateTs(){
    if($('ts')) $('ts').textContent = 'Updated ' + new Date().toLocaleTimeString();
  }

  // Monkey-patch loadData once it exists.
  function install(){
    if(typeof window.loadData !== 'function'){ return setTimeout(install, 60); }
    const _origLoad = window.loadData;
    window.loadData = async function(){
      const r = await _origLoad.apply(this, arguments);
      try{
        const res = await fetch('/api/claims');
        const d = await res.json();
        const claims = d.claims || [];
        renderHeroCards(claims);
        renderSidebar(claims);
        updateTs();
      }catch(e){ console.warn('augment fetch failed', e); }
      return r;
    };
    // Run once immediately so the UI does not stay blank on first load.
    window.loadData();
  }

  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', install);
  } else {
    install();
  }
})();
</script>
    <script>
        const STATE_LABELS = {{ state_labels | tojson }};
        const PIPELINE_STAGES = {{ pipeline_stages | tojson }};

        const TASK_TEMPLATES = {
            bs_missing_docs: JSON.stringify({
                claim_ids: [],
                redo: false,
                note: "eCW only: discovers the claims, generates the HCFA forms and captures the IV Notes / encounter files. claim_ids (or the 'Only these claims' box, comma-separated) limits the run to those claims, found on the eCW Claims page or opened by direct lookup; with claim_ids, redo defaults to true — everything is collected again for them even when a file is already stored."
            }, null, 2),
            blueshield_submissions: JSON.stringify({
                note: "Uploads claim documentation (HCFA, Prog Notes, Encounter File) to Blue Shield via SympliSend."
            }, null, 2),
            ecw_status_update: JSON.stringify({
                note: "Updates claim status in ECW from 'Ready to Submit to Symplisend' to 'Claim sent via Symplisend' for all submitted claims."
            }, null, 2),
            check_reconcile: JSON.stringify({
                since: "07/01/2025",
                blue_shield: false,
                limit_checks: 0,
                check_eft: "",
                copies: true,
                limit_files: 0,
                ecw: true,
                note: "Read-only. blue_shield:true walks the portal first (limit_checks caps the checks, check_eft names one); false reuses the checks already captured. copies:true reads new check images from SharePoint (Insurance Checks, through the bot's browser — sign in once on the live screen; or the sharepoint_credentials app registration), or with source:'s3' from s3://<bucket>/checks/inbox/; limit_files caps them. ecw:true logs into eCW and reads Billing → Payments since `since` (by Check # when the run is about a few checks). Then every check gets a verdict: posted / unposted / not in eCW / not cashed / copy only, and a flag when we hold no copy."
            }, null, 2),
            check_test_one: JSON.stringify({
                since: "07/01/2025",
                blue_shield: true,
                limit_checks: 1,
                check_eft: "",
                copies: true,
                limit_files: 10,
                ecw: true,
                note: "One check, end to end. Blue Shield: the first check in the results (or check_eft) is opened and its status read today. SharePoint (Insurance Checks, through the bot's browser — sign in once on the live screen when the log asks): the images named after it are read first, then up to limit_files more. eCW: Billing → Payments, Check # = the check, Lookup. Its row lands in the ✅ Checks table under the 🧪 Last run filter. Read-only; nothing is posted."
            }, null, 2)
        };

        // Maps a dropdown value into a different SQS payload {task_type, stage?}.
        // The test of 1 is the reconciliation with one check taken from the
        // portal first; every other task sends its own value as the task_type.
        const TASK_DISPATCH = { check_test_one: { task_type: 'check_reconcile' } };

        const TASK_DESCRIPTIONS = {
            bs_missing_docs: {
                title: 'ECW obtain claims documentation',
                desc: 'eCW only — nothing is sent to Blue Shield. Discovers the claims in eCW, generates the HCFA forms and captures the Progress Notes, and stores them. Uploading to Blue Shield is the separate task "Blueshield Submissions".',
                steps: ['eCW → Billing → Claims: discover the claims and store them', 'Generate the HCFA form for each claim', 'Capture the Progress Notes', 'Nothing is uploaded — run Blueshield Submissions for that', 'To redo specific claims: list them in claim_ids (or the box above) — their documents are collected again']
            },
            blueshield_submissions: {
                title: 'Blueshield Submissions',
                desc: 'Uploads claim documentation (HCFA, Progress Notes, Encounter File) to Blue Shield via SympliSend.',
                steps: []
            },
            ecw_status_update: {
                title: 'ECW Status Update',
                desc: "Updates claim status in ECW from 'Ready to Submit to Symplisend' to 'Claim sent via Symplisend' for all submitted claims.",
                steps: []
            },
            check_reconcile: {
                title: 'Reconcile checks',
                desc: 'Which checks are posted, which are not, which never reached eCW, and which we hold no copy of. Reads only: check images, the Blue Shield checks (walked now with blue_shield:true, else the ones captured before), and the eCW Payments list.',
                steps: ['Blue Shield (blue_shield:true): Claims → Check claim status → each Check/EFT: amount, status, cashed date', 'SharePoint → Insurance Checks: read each check image (number + amount)', 'Flag every cashed check we hold no copy of', 'eCW → Billing → Payments since 07/01/2025: on file = posted (or entered but unposted)', 'Verdict per check in the ✅ Checks table; CSV export']
            },
            check_test_one: {
                title: 'Test of 1 — one check, end to end',
                desc: 'The whole flow on a single check, to watch it on the live screen and see what the bot tracks. Same task as Reconcile checks, with blue_shield:true and limit_checks:1.',
                steps: ['Blue Shield → Claims → Check claim status → the first check in the results (or check_eft) → Check/EFT details: amount, status (Check Cashed?), cashed date', 'SharePoint (or the S3 inbox): the image named after that check is read first — number + amount — then up to limit_files more', 'eCW → Billing → Payments → Rcvd Pmt Dts from since → Check # = the check → Lookup: on file or not, posted / unposted', 'Its row in ✅ Checks (filter 🧪 Last run); the steps show above the tiles as they happen']
            }
        };


        // ── Run: what each bot can do, in plain words. Each action is a
        // task the raw panel (Advanced) can also send; the words are the
        // only thing added.
        const ACTIONS = {
            submissions: [
                {icon: '📋', title: 'Get documentation from eCW', task: 'bs_missing_docs',
                 desc: 'Finds the claims in eCW, generates each HCFA and captures the IV Note and Progress Note. Nothing is sent to Blue Shield.',
                 input: {key: 'claim_ids', list: true, placeholder: 'Only these claims, e.g. 6234, 3865 (empty = all)', extra: {redo: true}}},
                {icon: '📤', title: 'Upload to Blue Shield', task: 'blueshield_submissions',
                 desc: 'Sends every complete packet (HCFA, IV Note, Progress Note) through SympliSend.',
                 input: {key: 'test_claim_id', placeholder: 'Only this claim, e.g. 6234 (empty = all ready)', extra: {testing_mode: true}}},
                {icon: '📝', title: 'Mark sent claims in eCW', task: 'ecw_status_update',
                 desc: "Sets each submitted claim to 'Claim sent via Symplisend' in eCW."},
            ],
            eob: [
                {icon: '✅', title: 'Reconcile all checks', task: 'check_reconcile',
                 desc: 'Walks Blue Shield for new checks, reads new check images in SharePoint, looks each check up in eCW and refreshes every verdict. Reads only.',
                 payload: {since: '07/01/2025', blue_shield: true, limit_checks: 0, check_eft: '', copies: true, limit_files: 0, ecw: true}},
                {icon: '🧪', title: 'Test one check', task: 'check_reconcile',
                 desc: 'One check end to end: Blue Shield today, its copy in SharePoint, eCW. Its row lands under the Last run tile.',
                 payload: {since: '07/01/2025', blue_shield: true, limit_checks: 1, check_eft: '', copies: true, limit_files: 10, ecw: true},
                 input: {key: 'check_eft', placeholder: 'Check #, e.g. 766832992 (empty = the next new check)'}},
            ],
        };
        ACTIONS.resubmissions = ACTIONS.submissions;

        function renderActions(bot) {
            const el = document.getElementById('actions');
            if (!el) return;
            const list = ACTIONS[bot] || [];
            el.innerHTML = list.map((a, i) => `
                <div class="action">
                  <div class="action-t">${a.icon} ${a.title}</div>
                  <div class="action-d">${a.desc}</div>
                  <div class="action-row">
                    ${a.input ? `<input type="text" id="action-in-${i}" placeholder="${a.input.placeholder}">` : '<span style="flex:1"></span>'}
                    <button class="btn-run" onclick="runAction('${bot}', ${i})">Run</button>
                  </div>
                </div>`).join('');
        }

        async function runAction(bot, i) {
            const a = (ACTIONS[bot] || [])[i];
            if (!a) return;
            let payload = {};
            if (a.payload) payload = {...a.payload};
            else { try { payload = JSON.parse(TASK_TEMPLATES[a.task] || '{}'); } catch (e) { payload = {}; } }
            delete payload.note;
            const box = document.getElementById(`action-in-${i}`);
            const raw = box ? box.value.trim() : '';
            if (a.input && raw) {
                payload[a.input.key] = a.input.list ? raw.split(/[\s,;]+/).filter(Boolean) : raw;
                Object.assign(payload, a.input.extra || {});
            }
            payload.task_type = a.task;
            await postTask(payload, a.title);
        }

        async function postTask(payload, what) {
            payload.bot = window.activeBot;
            const res = await fetch('/api/send-task', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (data.success) {
                showToast(`${what || payload.task_type} started ✓`, 'success');
                // New run starting — wipe the panel so this run's logs are not
                // visually mixed with the previous run's output.
                clearLogs('new ' + (payload.task_type || 'task') + ' run');
                setTimeout(loadLogs, 3000);
            } else {
                showToast('Failed: ' + data.error, 'error');
            }
        }

        function updateTaskTemplate() {
            const type = document.getElementById('task-type').value;
            document.getElementById('task-payload').value = TASK_TEMPLATES[type] || '{}';
            renderTaskSteps(type);
        }

        function renderTaskSteps(type) {
            const el = document.getElementById('task-steps');
            const info = TASK_DESCRIPTIONS[type];
            if (!el || !info) { if (el) el.innerHTML = ''; return; }
            const stepsHtml = (info.steps && info.steps.length)
                ? `<ol>${info.steps.map(s => `<li>${s}</li>`).join('')}</ol>`
                : '';
            el.innerHTML = `
                <div class="task-steps-title">📋 ${info.title}</div>
                <div class="task-steps-desc">${info.desc}</div>
                ${stepsHtml}
            `;
        }

        // Active bot tab. Drives which SQS queue / systemd service the dashboard talks to.
        window.activeBot = 'submissions';

        const BOT_NOVNC = {{ bot_novnc | tojson }};
        const BOT_NAMES = {{ bot_names | tojson }};
        renderActions(window.activeBot);

        function setActiveBot(bot) {
            if (!(bot in BOT_NOVNC)) bot = 'submissions';
            window.activeBot = bot;
            // Each bot runs on its own X display behind its own noVNC port —
            // pointing every tab at 6080 would show the submissions bot's
            // screen while claiming to show another's.
            const vnc = document.getElementById('novnc-link');
            if (vnc) vnc.href = `http://54.189.175.233:${BOT_NOVNC[bot]}/vnc.html`;
            if (typeof syncLiveScreen === 'function') syncLiveScreen();
            // Tab styling
            document.querySelectorAll('#bot-tabs .subtab').forEach(el => {
                el.classList.toggle('on', el.dataset.bot === bot);
            });
            // Show/hide task-type optgroups + options
            document.querySelectorAll('#task-type [data-bot]').forEach(el => {
                const owners = (el.dataset.bot || '').split(/\s+/).filter(Boolean);
                const mine = owners.includes(bot);
                el.hidden = !mine;
                el.disabled = !mine;
            });
            // Pick the first visible option for this bot
            const sel = document.getElementById('task-type');
            const firstVisible = Array.from(sel.options).find(o => !o.hidden);
            if (firstVisible) { sel.value = firstVisible.value; updateTaskTemplate(); }
            renderActions(bot);
            const df = document.getElementById('date-filter');
            if (df) df.hidden = (bot === 'eob');
            const numLabel = document.getElementById('hero-num-label');
            if (numLabel) { numLabel.hidden = false; numLabel.textContent = bot === 'eob' ? ' need attention,' : ' still to send,'; }
            // The headline counts something different per bot: what each has
            // sent, or — for Remittance — how many checks the three sources
            // agree on (a copy on file, cashed, posted in eCW, amounts equal).
            const denom = document.getElementById('hero-denom-label');
            if (denom) denom.textContent = bot === 'eob'
                ? 'checks compared: copy · Blue Shield · eCW' : 'claims in eCW';
            const pctLabel = document.getElementById('hero-pct-label');
            if (pctLabel) pctLabel.textContent = bot === 'eob' ? 'posted & matching' : 'sent to SympliSend';
            const remLabel = document.getElementById('hero-remaining-label');
            if (remLabel) remLabel.textContent = bot === 'eob' ? 'eCW not checked yet' : 'sent';
            // Remittance works per check, not per claim document — the claims
            // table's HCFA / IV note / progress-note columns mean nothing
            // there — so each tab shows exactly one main panel.
            const chkSec = document.getElementById('checks-section');
            if (chkSec) chkSec.hidden = (bot !== 'eob');
            const claimsSec = document.getElementById('claims-section-submissions');
            if (claimsSec) claimsSec.hidden = (bot === 'eob');
            if (bot === 'eob' && typeof loadChecks === 'function') loadChecks();
            const fldSec = document.getElementById('folders-section');
            if (fldSec) fldSec.hidden = (bot !== 'eob');
            if (bot === 'eob' && typeof loadFolders === 'function') loadFolders();
            // Re-render from the claims already loaded, THEN refetch. The
            // table used to keep showing the previous tab's claims until the
            // next full fetch came back.
            if (window._allClaims) {
                const cached = applyDateFilter(claimsForActiveBot(window._allClaims));
                renderStats(cached);
                renderPipeline(cached);
                renderClaims(cached);
            }
            if (typeof loadCounts === 'function') loadCounts();
            if (typeof loadData === 'function') loadData();
            // Refresh logs for the new service
            clearLogs('switched to ' + bot);
            loadLogs();
        }

        

        async function sendTask() {
            const selected = document.getElementById('task-type').value;
            let payload;
            try {
                payload = JSON.parse(document.getElementById('task-payload').value);
            } catch (e) {
                showToast('Invalid JSON payload', 'error');
                return;
            }
            // Translate the dropdown value into {task_type, stage?} via TASK_DISPATCH if mapped,
            // otherwise the dropdown value IS the task_type.
            const dispatch = TASK_DISPATCH[selected];
            if (dispatch) {
                payload.task_type = dispatch.task_type;
                if (dispatch.stage !== undefined) payload.stage = dispatch.stage;
            } else {
                payload.task_type = selected;
            }
            // Add test claim ID if specified
            const testClaimId = (document.getElementById('test-claim-id').value || '').trim();
            if (testClaimId) {
                payload.test_claim_id = testClaimId;
                payload.testing_mode = true;
            }
            await postTask(payload, payload.task_type);
        }

        async function deleteAllClaims() {
            if (!confirm('⚠️ DELETE all claims from the database? This cannot be undone. The next pipeline run will rediscover claims from ECW.')) return;
            try {
                const res = await fetch('/api/delete-all-claims', { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    showToast(data.message + ' \\u2713', 'success');
                    setTimeout(loadData, 500);
                } else {
                    showToast('Delete failed: ' + data.error, 'error');
                }
            } catch (e) {
                showToast('Delete failed: ' + e.message, 'error');
            }
        }

        // ── Stage classification for each claim ──
        function getStageKey(state) {
            for (const [key, stage] of Object.entries(PIPELINE_STAGES)) {
                if (stage.states.includes(state)) return key;
            }
            return 'documentation';
        }

        function getStagePill(state, c) {
            const key = getStageKey(state);
            let label = PIPELINE_STAGES[key]?.label || 'Unknown';
            // A claim in the documentation stage with nothing stored — no
            // HCFA, no IV Note — is not documented yet, whatever its state
            // number says (2026-09-19: 24 IV claims read "Documentation
            // Completed" with every column empty).
            if (key === 'documentation' && c && !c.hcfa_s3_path && !c.prog_notes_s3_path) {
                return `<span class="state-pill pending" title="State ${state}: no HCFA and no IV Note stored yet — run Get documentation from eCW with this claim's number">Documentation Pending</span>`;
            }
            // Something is still missing from the packet: say which, not "completed".
            if (key === 'documentation' && c) {
                const isOffice = !!c.office_visit || /\b(9920[1-5]|9921[1-5])\b/.test(String(c.cpt || ''));
                const missing = [!c.hcfa_s3_path ? 'HCFA' : '', !c.prog_notes_s3_path ? 'IV Note' : '',
                                 (!isOffice && !c.encounter_file_s3_path && !c.progress_note_not_required) ? 'Progress Note' : ''].filter(Boolean);
                if (missing.length) return `<span class="state-pill pending" title="State ${state}">${missing.join(' + ')} missing</span>`;
            }
            return `<span class="state-pill ${key}">${label}</span>`;
        }

        // Manual override: flag a claim as not needing a Progress Note (e.g.,
        // diagnostic-only CPT mix). Posts to /api/claim/<cid>/skip_progress_note.
        async function skipProgressNote(claimId, skip) {
            const action = skip ? 'mark as NOT needing a Progress Note' : 'undo the override';
            if (!confirm(`Claim ${claimId}: ${action}?`)) return;
            try {
                const res = await fetch(`/api/claim/${claimId}/skip_progress_note`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ skip: skip }),
                });
                const data = await res.json();
                if (!res.ok) { alert('Failed: ' + (data.error || res.status)); return; }
                loadData();
            } catch (e) {
                alert('Error: ' + e.message);
            }
        }

        // Manual upload of a progress note PDF for a claim that the bot couldn't
        // capture. Opens a file picker, POSTs to /api/encounter_file/<cid>/upload.
        function uploadEncounterFile(claimId) {
            const input = document.createElement('input');
            input.type = 'file';
            input.accept = 'application/pdf,.pdf';
            input.style.display = 'none';
            document.body.appendChild(input);
            input.onchange = async () => {
                const file = input.files && input.files[0];
                document.body.removeChild(input);
                if (!file) return;
                if (!file.name.toLowerCase().endsWith('.pdf')) {
                    alert('Please choose a PDF file.');
                    return;
                }
                const fd = new FormData();
                fd.append('file', file);
                try {
                    const res = await fetch(`/api/encounter_file/${claimId}/upload`, { method: 'POST', body: fd });
                    const data = await res.json();
                    if (!res.ok) { alert('Upload failed: ' + (data.error || res.status)); return; }
                    alert(`✅ Uploaded progress note for claim ${claimId} (${data.size} bytes)`);
                    loadData();  // refresh table
                } catch (e) {
                    alert('Upload error: ' + e.message);
                }
            };
            input.click();
        }

        // Progress Note Date — prefer the Rx Start Date parsed from the IV Note
        // (iv_note_rx_start_date, stored as YYYY-MM-DD). This is the date used to
        // look up the progress note in ECW, and it's available even when the
        // encounter PDF capture failed. Fall back to encounter_date (MM/DD/YYYY)
        // when no IV Note Rx date was parsed yet. Cell is clickable to edit.
        function formatProgNoteDate(c) {
            const rx = c.iv_note_rx_start_date;
            let shown = '—';
            if (rx) {
                const m = String(rx).match(/^(\d{4})-(\d{2})-(\d{2})/);
                shown = m ? `${m[2]}/${m[3]}/${m[1]}` : rx;
            } else if (c.encounter_date) {
                shown = c.encounter_date;
            }
            const cid = c.claim_id;
            return `<span style="cursor:pointer;" onclick="editProgNoteDate('${cid}', '${rx || ''}')" title="Click to edit">${shown} <span style="color:var(--info);font-size:9px;">✏️</span></span>`;
        }

        // Manual edit of Progress Note Date (iv_note_rx_start_date in DDB).
        // Prompts user for MM/DD/YYYY, POSTs to /api/claim/<cid>/prog_note_date.
        async function editProgNoteDate(claimId, currentIso) {
            // Show current as MM/DD/YYYY in the prompt for clarity.
            let prefill = '';
            if (currentIso) {
                const m = String(currentIso).match(/^(\d{4})-(\d{2})-(\d{2})/);
                if (m) prefill = `${m[2]}/${m[3]}/${m[1]}`;
                else prefill = currentIso;
            }
            const val = prompt(`Progress Note Date for claim ${claimId} (MM/DD/YYYY, blank to clear):`, prefill);
            if (val === null) return;  // cancel
            try {
                const res = await fetch(`/api/claim/${claimId}/prog_note_date`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ date: val.trim() }),
                });
                const data = await res.json();
                if (!res.ok) { alert('Update failed: ' + (data.error || res.status)); return; }
                loadData();
            } catch (e) {
                alert('Update error: ' + e.message);
            }
        }

        // New submissions and resubmissions are separate bots with separate
        // queues, displays and browser profiles, so each tab shows only its own
        // claims. Anything not explicitly marked a resubmission counts as a new
        // submission — an unclassified claim belongs with the first-time work.
        function claimsForActiveBot(claims) {
            const isResub = c => /resub/i.test(c.submission_type || '');
            if (window.activeBot === 'resubmissions') return claims.filter(isResub);
            if (window.activeBot === 'submissions') return claims.filter(c => !isResub(c));
            if (window.activeBot === 'eob') return [];  // Remittance shows checks, not claims
            return claims;
        }

        // ---- MFA relay ----
        // Blue Shield e-mails a 2-step code at every fresh login; when Gmail
        // cannot be read, the bot waits a few minutes for one typed here.
        async function sendMfaCode() {
            const inp = document.getElementById('mfa-code');
            const st = document.getElementById('mfa-status');
            const code = (inp.value || '').replace(/\D/g, '');
            if (code.length < 4) { st.textContent = 'type the digits first'; return; }
            st.textContent = 'sending…';
            try {
                const res = await fetch('/api/mfa-code', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({code})
                });
                const data = await res.json();
                st.textContent = data.success ? `✅ code delivered ${data.submitted_at}` : ('❌ ' + (data.error || 'failed'));
                if (data.success) inp.value = '';
            } catch (e) {
                st.textContent = '❌ ' + e;
            }
        }

        // ---- Live screen (noVNC, embedded) ----
        // Follows the active bot tab: Intake :6080, Follow-up :6081,
        // Remittance :6083. Interactive: a person can take over the bot's browser
        // (type a code, close a dialog) — the bot is not paused by it.
        function liveScreenUrl() {
            const bot = window.activeBot || 'submissions';
            const port = BOT_NOVNC[bot] || 6080;
            return `http://54.189.175.233:${port}/vnc.html?autoconnect=true&resize=scale&reconnect=true`;
        }
        function syncLiveScreen() {
            const panel = document.getElementById('live-screen');
            if (!panel || panel.hidden) return;
            const url = liveScreenUrl();
            const fr = document.getElementById('live-screen-frame');
            if (fr && fr.src !== url) fr.src = url;
            const a = document.getElementById('live-screen-open');
            if (a) a.href = url;
            const t = document.getElementById('live-screen-title');
            const name = (typeof BOT_NAMES !== 'undefined' && BOT_NAMES[window.activeBot]) || window.activeBot || '';
            if (t) t.textContent = `🖥️ Live screen · ${name}`;
        }
        function toggleLiveScreen() {
            const panel = document.getElementById('live-screen');
            if (!panel) return;
            panel.hidden = !panel.hidden;
            if (panel.hidden) {
                const fr = document.getElementById('live-screen-frame');
                if (fr) fr.src = 'about:blank';
            } else {
                syncLiveScreen();
            }
            try { localStorage.setItem('liveScreen', panel.hidden ? '0' : '1'); } catch (e) {}
        }
        try { if (localStorage.getItem('liveScreen') === '1') setTimeout(toggleLiveScreen, 300); } catch (e) {}

        // ---- Checks (Remittance) ----
        // One row per check number: the copy we hold, Blue Shield's word,
        // eCW's payment, and the verdict. Filters are client-side.
        window._checksFilter = window._checksFilter || 'mismatch';
        const CHECK_FILTERS = [['last run', '🧪 Last run'], ['mismatch', '⚠ Needs attention'], ['all', 'All'], ['posted', 'Posted'], ['unposted', 'Unposted'],
                               ['not in eCW', 'Not in eCW'], ['not cashed', 'Not cashed'], ['eCW not checked', 'eCW not checked'],
                               ['other payer', 'Other payer'], ['no copy', 'No copy'], ['amounts', 'Amounts differ']];
        // "Needs attention" — the same definition as the team view: what a
        // person must act on. Cashed but not in eCW, entered but unposted, a
        // cashed check with no copy, amounts that disagree. Copy-only checks
        // (other payers), not-yet-cashed ones and an unread eCW are shown in
        // their own tiles, not counted here.
        const checkMismatch = r => ['not in eCW', 'unposted'].includes(r.verdict)
            || (r.flags || []).some(f => String(f).startsWith('no copy') || String(f).startsWith('amounts differ'));
        // The rows the last run wrote: its targets (a test of 1), else
        // whatever carries the last reconciliation's timestamp.
        const inLastRun = (r, sm) => {
            const lr = sm.last_run || {};
            if ((lr.targets || []).length) return lr.targets.includes(String(r.check_number));
            return !!sm.reconciled_at && r.reconciled_at === sm.reconciled_at;
        };
        const RUN_STEPS = [['blue_shield', 'Blue Shield'], ['copies', 'Copies'], ['ecw', 'eCW'], ['verdict', 'Verdict'], ['done', 'Done']];
        function setChecksFilter(f) { window._checksFilter = f; window._checksFilterChosen = true; renderChecks(); }
        function renderChecks() {
            const body = document.getElementById('checks-body');
            const meta = document.getElementById('checks-meta');
            const filt = document.getElementById('checks-filters');
            const data = window._checksData || {rows: [], summary: {}};
            const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
            const sm = data.summary || {};
            const lr = sm.last_run || {};
            // After a targeted run the tab opens on its check(s); the
            // operator's own choice of filter sticks for the session.
            if (!window._checksFilterChosen) window._checksFilter = (lr.targets || []).length ? 'last run' : 'mismatch';
            const mism = data.rows.filter(checkMismatch).length;
            const matching = data.rows.filter(r => r.verdict === 'posted' && !(r.flags || []).some(f => f !== 'other payer')).length;
            const nf = n => Number(n || 0).toLocaleString('en-US');
            if (meta) meta.textContent = data.rows.length
                ? `${nf(sm.checks)} checks · ${sm.reconciled_at ? 'last reconciled ' + sm.reconciled_at : 'no reconciliation yet'}` : '';
            // The headline: what a person must act on, out of everything compared.
            if (window.activeBot === 'eob') {
                const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
                const total = data.rows.length;
                set('hero-submitted', nf(mism));
                set('hero-total', nf(total));
                set('hero-pct', (total ? Math.round(matching / total * 100) : 0) + '%');
                set('hero-pct-label', 'posted & matching');
                set('hero-remaining', nf(sm.ecw_unchecked));
                set('hero-remaining-label', 'eCW not checked yet');
                const fill = document.getElementById('hero-progress-fill');
                if (fill) fill.style.width = (total ? Math.round(matching / total * 100) : 0) + '%';
            }
            const runEl = document.getElementById('checks-run');
            if (runEl) {
                const running = lr.started_at && !(lr.steps || {}).done;
                const chip = (label, text) => `<span style="display:inline-block;margin-right:10px"><strong>${esc(label)}:</strong> ${esc(text)}</span>`;
                runEl.innerHTML = lr.started_at ? [
                    `<span style="display:inline-block;margin-right:10px">${running ? '⏳ <strong>Run in progress</strong>' : '✔ <strong>Last run</strong>'} · ${esc(lr.mode || 'run')}${(lr.targets || []).length ? ' · check ' + esc(lr.targets.join(', ')) : ''} · ${esc(lr.started_at)}</span>`,
                    ...RUN_STEPS.filter(([k]) => (lr.steps || {})[k] && k !== 'done').map(([k, label]) => chip(label, lr.steps[k])),
                ].join('') : '';
            }
            // The tiles are the filter: one click, one question.
            const tiles = document.getElementById('checks-tiles');
            if (tiles) {
                const tile = (label, n, f, color, meaning) => `<div class="chk-tile${window._checksFilter === f ? ' on' : ''}" onclick="setChecksFilter('${f}')" style="border-color:${window._checksFilter === f ? color : 'var(--bdr)'}"><div class="chk-tile-n" style="color:${color}">${nf(n)}</div><div class="chk-tile-l">${label}</div>${meaning ? `<div class="chk-tile-m">${meaning}</div>` : ''}</div>`;
                tiles.innerHTML = data.rows.length ? [
                    tile('need attention', mism, 'mismatch', 'var(--bad)', 'a person must act'),
                    tile('cashed, not in eCW', sm.not_in_ecw, 'not in eCW', 'var(--bad)', 'enter the payment'),
                    tile('entered, unposted', sm.unposted, 'unposted', 'var(--warning)', 'finish posting'),
                    tile('no copy of the check', sm.no_copy, 'no copy', 'var(--bad)', 'scan it into SharePoint'),
                    tile('amounts differ', sm.amount_mismatch, 'amounts', 'var(--warning)', 'check which is right'),
                    tile('posted & matching', matching, 'posted', 'var(--success)', 'nothing to do'),
                    tile('not cashed yet', sm.not_cashed, 'not cashed', 'var(--text-muted)', 'wait for the bank'),
                    tile('other payer', sm.other_payer, 'other payer', 'var(--text-muted)', 'not a Blue Shield check'),
                    tile('eCW not checked', sm.ecw_unchecked, 'eCW not checked', 'var(--info)', 'run again'),
                    ...((lr.targets || []).length ? [tile('last run', data.rows.filter(r => inLastRun(r, sm)).length, 'last run', 'var(--vio2)', 'the test just run')] : []),
                    tile('all checks', data.rows.length, 'all', 'var(--text-secondary)', ''),
                ].join('') : '';
                if (sm.unreadable) tiles.innerHTML += `<div class="chk-tile" title="${esc((sm.unreadable_files || []).map(u => u.file + ' — ' + u.problem).join(' | '))}" style="border-color:var(--bdr);cursor:default"><div class="chk-tile-n" style="color:var(--warning)">${nf(sm.unreadable)}</div><div class="chk-tile-l">scans not read</div><div class="chk-tile-m">tried again next run</div></div>`;
            }
            if (!body) return;
            const f = window._checksFilter;
            const rows = data.rows.filter(r => f === 'all' ? true
                : f === 'last run' ? inLastRun(r, sm)
                : f === 'mismatch' ? checkMismatch(r)
                : f === 'posted' ? (r.verdict === 'posted' && !(r.flags || []).some(x => x !== 'other payer'))
                : f === 'no copy' ? (r.flags || []).some(x => x.startsWith('no copy'))
                : f === 'other payer' ? (r.flags || []).some(x => x === 'other payer')
                : f === 'amounts' ? (r.flags || []).some(x => x.startsWith('amounts differ'))
                : r.verdict === f);
            if (!rows.length) {
                body.innerHTML = `<tr><td colspan="7" class="empty-state">${data.rows.length ? 'Nothing under this tile.' : 'No reconciliation yet. Use <strong>▶ Run → Reconcile all checks</strong> (or <strong>Test one check</strong>) on the right. If the log stops at Blue Shield’s 2-step, type the e-mailed code in the <strong>Blue Shield code</strong> box.'}</td></tr>`;
                return;
            }
            const color = v => v === 'posted' ? 'var(--success)' : v === 'unposted' ? 'var(--warning)' : v === 'not in eCW' ? 'var(--bad)' : v === 'eCW not checked' ? 'var(--info)' : 'var(--text-muted)';
            const money = v => (v === '' || v == null) ? '' : '$' + Number(String(v).replace(/[^0-9.-]/g, '')).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
            const todo = r => {
                const out = [];
                const flags = r.flags || [];
                if (r.verdict === 'not in eCW') out.push(`<strong>Enter the payment in eCW</strong>${r.has_copy ? ` — the scan is in ${esc(r.copy_folder || 'SharePoint')}` : ''}.`);
                if (r.verdict === 'unposted') out.push(`<strong>Finish posting</strong>${r.ecw_unposted ? ' ' + money(r.ecw_unposted) : ''} in eCW.`);
                if (flags.some(x => x.startsWith('no copy'))) out.push('<strong>Scan the check</strong> into SharePoint › Insurance Checks.');
                if (flags.some(x => x.startsWith('amounts differ'))) out.push('<strong>Check which amount is right</strong>: copy, Blue Shield and eCW disagree.');
                if (r.verdict === 'eCW not checked') out.push('eCW was not read for this check yet — <strong>run again</strong>.');
                if (r.verdict === 'not cashed') out.push('Issued, not cashed — wait for the bank.');
                if (!out.length) out.push('Nothing — the sources agree.');
                return out.join('<br>');
            };
            body.innerHTML = rows.map(r => {
                const amounts = [['copy', r.copy_amount], ['Blue Shield', r.bs_amount], ['eCW', r.ecw_amount]].filter(([, v]) => v);
                const differ = (r.flags || []).some(x => x.startsWith('amounts differ'));
                const amountCell = !amounts.length ? '<span style="color:var(--text-muted)">—</span>'
                    : differ ? amounts.map(([k, v]) => `<div style="font-size:11px">${money(v)} <span style="color:var(--text-muted)">${k}</span></div>`).join('')
                    : `<strong>${money(amounts[0][1])}</strong>`;
                const copyCell = r.has_copy
                    ? (r.copy_url ? `<a href="${esc(r.copy_url)}" target="_blank" title="${esc(r.copy_file)}">${esc(r.copy_folder || '')}</a>` : `<span title="${esc(r.copy_file)}">${esc(r.copy_folder || '')}</span>`)
                      + `<div style="font-size:11px;color:var(--text-muted)">${esc(String(r.copy_file || '').split('/').pop())}${r.deposit_file ? ` · 🏦 deposit${r.deposit_date ? ' ' + esc(r.deposit_date) : ''} ${money(r.deposit_total)}${r.copy_page ? ' · p.' + esc(String(r.copy_page)) : ''}` : ''}</div>`
                    : '<span style="color:var(--bad);font-weight:600">✗ no copy</span>';
                const bsCell = r.in_blue_shield
                    ? `${esc(r.bs_status || '—')}${r.cashed_date ? `<div style="font-size:11px;color:var(--text-muted)">cashed ${esc(r.cashed_date)}</div>` : r.bs_date ? `<div style="font-size:11px;color:var(--text-muted)">issued ${esc(r.bs_date)}</div>` : ''}`
                    : '<span style="color:var(--text-muted)">other payer</span>';
                const ecwCell = r.in_ecw
                    ? `on file${r.ecw_payment_id ? ' · #' + esc(r.ecw_payment_id) : ''}<div style="font-size:11px;color:var(--text-muted)">${r.ecw_posted ? 'posted ' + money(r.ecw_posted) : ''}${r.ecw_unposted && Number(r.ecw_unposted) > 0 ? ` · <span style="color:var(--warning)">unposted ${money(r.ecw_unposted)}</span>` : ''}</div>`
                    : r.verdict === 'eCW not checked' ? '<span style="color:var(--info)">not checked</span>' : '<span style="color:var(--text-muted)">not found</span>';
                return `
                <tr${inLastRun(r, sm) ? ' style="background:rgba(99,102,241,.06)"' : ''}>
                  <td title="checked ${esc(r.reconciled_at || '')}"><strong>${esc(r.check_full || r.check_number)}</strong>${inLastRun(r, sm) ? ' <span title="in the last run">🧪</span>' : ''}</td>
                  <td>${copyCell}</td>
                  <td class="num">${amountCell}</td>
                  <td>${bsCell}</td>
                  <td>${ecwCell}</td>
                  <td><span class="verdict-pill" style="color:${color(r.verdict)}">● ${esc(r.verdict)}</span></td>
                  <td class="todo">${todo(r)}</td>
                </tr>`;
            }).join('');
        }
        async function loadChecks() {
            try {
                const res = await fetch('/api/checks');
                window._checksData = await res.json();
                renderChecks();
            } catch (e) { console.error('loadChecks failed', e); }
        }

        // ---- Date filter (hero card) ----
        // Filters everything the page shows — the headline counter, the
        // pipeline, the stats and the claims table — by service date or by
        // the date the packet was sent. Claims carry dates in two shapes:
        // dos "MM/DD/YYYY" and symplisend_submitted_at "YYYY-MM-DD HH:MM:SS
        // UTC"; both normalise to ISO so <input type=date> values compare as
        // plain strings.
        function _claimDateISO(c, field) {
            if (field === 'sent') {
                const m = String(c.symplisend_submitted_at || '').match(/^(\d{4}-\d{2}-\d{2})/);
                return m ? m[1] : '';
            }
            const m = String(c.dos || c.service_date || '').match(/(\d{2})\/(\d{2})\/(\d{4})/);
            return m ? `${m[3]}-${m[1]}-${m[2]}` : '';
        }

        function dateFilterActive() {
            return !!((document.getElementById('df-from') || {}).value
                      || (document.getElementById('df-to') || {}).value);
        }

        function applyDateFilter(claims) {
            const from = (document.getElementById('df-from') || {}).value || '';
            const to = (document.getElementById('df-to') || {}).value || '';
            if (!from && !to) return claims;
            const field = (document.getElementById('df-field') || {}).value || 'dos';
            return claims.filter(c => {
                const d = _claimDateISO(c, field);
                if (!d) return false;
                if (from && d < from) return false;
                if (to && d > to) return false;
                return true;
            });
        }

        function onDateFilterChange() {
            const bar = document.getElementById('date-filter');
            if (bar) bar.classList.toggle('active', dateFilterActive());
            // Re-render from the cached load; a filter change should not cost
            // a 3MB refetch.
            if (window._allClaims) {
                const claims = applyDateFilter(claimsForActiveBot(window._allClaims));
                renderStats(claims);
                renderPipeline(claims);
                renderClaims(claims);
            } else {
                loadData();
            }
            loadCounts();
        }

        function clearDateFilter() {
            for (const id of ['df-from', 'df-to']) {
                const el = document.getElementById(id);
                if (el) el.value = '';
            }
            onDateFilterChange();
        }

        // The claims headline (2026-09-20): what is still to send, out of the
        // claims in eCW — the table below shows exactly those; the sent ones
        // are the bar and the small print.
        function paintClaimsHero(done, total) {
            const nf = n => Number(n || 0).toLocaleString('en-US');
            const pct = total ? Math.round((done / total) * 100) : 0;
            const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
            set('hero-submitted', nf(total - done));
            set('hero-total', nf(total));
            set('hero-denom-label', 'claims in eCW');
            set('hero-pct', pct + '%');
            set('hero-pct-label', 'sent to SympliSend');
            set('hero-remaining', nf(done));
            set('hero-remaining-label', 'sent');
            const lbl = document.getElementById('hero-num-label');
            if (lbl) { lbl.hidden = false; lbl.textContent = ' still to send,'; }
            const fill = document.getElementById('hero-progress-fill');
            if (fill) fill.style.width = pct + '%';
        }

        async function loadCounts() {
            if (window.activeBot === 'eob') return;   // renderChecks writes that headline
            // With a date filter on, the headline is computed from the cached
            // claims with the SAME state definition the server uses
            // (PIPELINE_STAGES.submitted) — one definition, two callers.
            if (dateFilterActive() && window._allClaims) {
                const rows = applyDateFilter(claimsForActiveBot(window._allClaims));
                const sub = PIPELINE_STAGES.submitted.states;
                const done = rows.filter(c => sub.includes(parseInt(c.state || 0))).length;
                paintClaimsHero(done, rows.length);
                const hint = document.getElementById('df-hint');
                if (hint) hint.textContent = 'filtered';
                return;
            }
            try {
                const res = await fetch('/api/claim-counts');
                const data = await res.json();
                const c = data[window.activeBot];
                if (!c || !c.total) return;
                paintClaimsHero(c.submitted, c.total);
            } catch (e) { /* the next tick will retry */ }
        }

        async function loadData() {
            try {
                const res = await fetch('/api/claims');
                const data = await res.json();
                window._allClaims = data.claims;
                const claims = applyDateFilter(claimsForActiveBot(data.claims));
                renderStats(claims);
                renderPipeline(claims);
                renderClaims(claims);
                renderTasks(data.tasks);
            } catch (e) {
                console.error('Failed to load data:', e);
            }
        }

        // Incremental log rendering — only APPEND new lines instead of replacing
        // the whole panel on every poll. Stops the 5s flicker that wiped any text
        // the user was selecting / reading. State:
        //   _logSeen        : Set of recently-rendered lines (de-dupe)
        //   _logRunAnchor   : timestamp set when a new task is sent; lines older
        //                    than this are filtered out so a fresh run starts clean
        let _logSeen = new Set();
        let _logRunAnchor = 0;
        const _MAX_LOG_LINES = 800;
        const _MAX_SEEN = 1500;

        function clearLogs(reason) {
            const body = document.getElementById('logs-body');
            if (body) body.innerHTML = `<div class="log-line info">— logs cleared (${reason}) —</div>`;
            _logSeen = new Set();
            _logRunAnchor = Date.now();
        }

        // Best-effort parse of journalctl's "May 21 03:25:11" prefix → ms timestamp.
        function _parseLogTs(line) {
            const m = (line || '').match(/^([A-Z][a-z]{2})\s+(\d+)\s+(\d{2}):(\d{2}):(\d{2})/);
            if (!m) return 0;
            const months = {Jan:0,Feb:1,Mar:2,Apr:3,May:4,Jun:5,Jul:6,Aug:7,Sep:8,Oct:9,Nov:10,Dec:11};
            const now = new Date();
            const d = new Date(now.getFullYear(), months[m[1]] || 0, parseInt(m[2]),
                               parseInt(m[3]), parseInt(m[4]), parseInt(m[5]));
            return d.getTime();
        }

        async function loadLogs() {
            try {
                const res = await fetch('/api/logs?bot=' + encodeURIComponent(window.activeBot || 'submissions'));
                const data = await res.json();
                const body = document.getElementById('logs-body');
                if (!body) return;
                // First load: clear the "Loading..." placeholder.
                if (body.firstElementChild && body.firstElementChild.textContent === 'Loading logs...') {
                    body.innerHTML = '';
                }
                // Preserve user's scroll position unless they're pinned to the bottom.
                const atBottom = (body.scrollHeight - body.scrollTop - body.clientHeight) < 40;
                const frag = document.createDocumentFragment();
                let added = 0;
                for (const line of (data.logs || [])) {
                    if (_logSeen.has(line)) continue;
                    if (_logRunAnchor && _parseLogTs(line) > 0 && _parseLogTs(line) < _logRunAnchor) continue;
                    _logSeen.add(line);
                    let cls = 'info';
                    if (line.includes('ERROR')) cls = 'error';
                    else if (line.includes('WARNING')) cls = 'warning';
                    else if (line.includes('complete') || line.includes('SUCCESS')) cls = 'success';
                    const div = document.createElement('div');
                    div.className = 'log-line ' + cls;
                    div.textContent = line;
                    frag.appendChild(div);
                    added++;
                }
                if (added > 0) {
                    body.appendChild(frag);
                    // Trim the panel — keep newest _MAX_LOG_LINES only.
                    while (body.childElementCount > _MAX_LOG_LINES) body.removeChild(body.firstElementChild);
                    // Trim the seen-set so it doesn't grow forever.
                    if (_logSeen.size > _MAX_SEEN) {
                        const arr = Array.from(_logSeen);
                        _logSeen = new Set(arr.slice(arr.length - _MAX_SEEN / 2));
                    }
                    if (atBottom) body.scrollTop = body.scrollHeight;
                }
            } catch (e) {
                console.error('Failed to load logs:', e);
            }
        }

        function renderStats(claims) {
            const getState = c => parseInt(c.state || c.current_state || 0);
            const submittedStates = PIPELINE_STAGES.submitted.states;

            if (window.activeBot === 'eob') return;   // renderChecks writes that headline
            const total = claims.length;
            const submittedCount = claims.filter(c => submittedStates.includes(getState(c))).length;
            paintClaimsHero(submittedCount, total);
        }

        function renderPipeline(claims) {
            const grid = document.getElementById('pipeline-grid');
            if (!grid) return;
            const getState = c => parseInt(c.state || c.current_state || 0);
            grid.innerHTML = '';

            const stageKeys = Object.keys(PIPELINE_STAGES);
            stageKeys.forEach((key, idx) => {
                const stage = PIPELINE_STAGES[key];
                const stageClaims = claims.filter(c => stage.states.includes(getState(c)));
                const count = stageClaims.length;

                // Build detail chips showing internal state breakdown
                const breakdown = {};
                for (const c of stageClaims) {
                    const s = getState(c);
                    const label = STATE_LABELS[s] || `State ${s}`;
                    breakdown[label] = (breakdown[label] || 0) + 1;
                }
                const detailChips = Object.entries(breakdown).map(([label, cnt]) =>
                    `<span class="detail-chip has-count">${cnt} ${label}</span>`
                ).join('');

                const arrow = idx < stageKeys.length - 1
                    ? '<div class="stage-arrow">→</div>'
                    : '';

                grid.innerHTML += `
                    <div class="pipeline-stage" onclick="openBSDrawer('${key}', '${stage.label}')">
                        <div class="stage-count" style="color: ${stage.color};">${count}</div>
                        <div class="stage-name">${stage.label}</div>
                        <div class="stage-bar" style="background: ${stage.color};"></div>
                        ${detailChips ? `<div class="stage-details">${detailChips}</div>` : ''}
                        ${arrow}
                    </div>`;
            });
        }

        // ---- Claims search (table only) ----
        // Filters just the table, not the headline or the pipeline: search is
        // for finding a row, the date filter is for scoping the numbers.
        // Client-side over the cached load, so clearing the box restores every
        // row instantly — the audit page's slow-response race cannot happen.
        function applyClaimsSearch(claims) {
            const q = ((document.getElementById('claims-search') || {}).value || '')
                .trim().toLowerCase();
            if (!q) return claims;
            const terms = q.split(/\s+/);
            return claims.filter(c => {
                const hay = [c.claim_id, c.patient_name, c.dos, c.service_date,
                             c.original_ref_no, c.subscriber_id, c.claim_status,
                             c.ecw_status_code, c.submission_type, c.cpt]
                    .map(v => String(v || '').toLowerCase()).join(' ');
                return terms.every(t => hay.includes(t));
            });
        }

        function toggleSubmitted() {
            window._showSubmitted = !window._showSubmitted;
            try { localStorage.setItem('showSubmitted', window._showSubmitted ? '1' : '0'); } catch (e) {}
            if (window._allClaims) renderClaims(applyDateFilter(claimsForActiveBot(window._allClaims)));
        }
        try { window._showSubmitted = localStorage.getItem('showSubmitted') === '1'; } catch (e) { window._showSubmitted = false; }

        let _claimsSearchTimer = null;
        function onClaimsSearch() {
            clearTimeout(_claimsSearchTimer);
            _claimsSearchTimer = setTimeout(() => {
                if (window._allClaims) {
                    renderClaims(applyDateFilter(claimsForActiveBot(window._allClaims)));
                } else {
                    loadData();
                }
            }, 150);
        }

        function renderClaims(claims) {
            const body = document.getElementById('claims-body');
            const meta = document.getElementById('claims-meta');
            // Claims already sent to SympliSend are done: out of the table
            // unless asked for (2026-09-20), so what is left is what needs work.
            const submittedN = claims.filter(c => c.symplisend_submitted).length;
            const tog = document.getElementById('submitted-toggle');
            if (tog) { tog.textContent = (window._showSubmitted ? 'Hide' : 'Show') + ` submitted (${submittedN})`; tog.classList.toggle('on', !!window._showSubmitted); }
            if (!window._showSubmitted) claims = claims.filter(c => !c.symplisend_submitted);
            const beforeSearch = claims.length;
            claims = applyClaimsSearch(claims);
            const searching = claims.length !== beforeSearch;
            if (!claims.length) {
                body.innerHTML = searching
                    ? '<tr><td colspan="8" class="empty-state">No claims match the search.</td></tr>'
                    : submittedN ? `<tr><td colspan="8" class="empty-state">Every claim here is already sent to SympliSend (${submittedN}). <a href="#" onclick="toggleSubmitted();return false;">Show them</a>.</td></tr>`
                    : '<tr><td colspan="8" class="empty-state">No claims yet. Use <strong>▶ Run → Get documentation from eCW</strong> on the right.</td></tr>';
                if (meta) meta.textContent = searching ? `0 of ${beforeSearch} claims match` : '';
                return;
            }
            if (searching && meta) {
                meta.textContent = `${claims.length} of ${beforeSearch} claims match`;
            }
            // Show metadata about the last scrape
            if (meta) {
                const scraped = claims.find(c => c.scraped_at);
                meta.textContent = scraped ? `Last scraped: ${scraped.scraped_at}` : '';
            }
            // Find the single most-recent processing_at across ALL claims. The bot
            // processes claims serially, so only the claim with the LATEST timestamp
            // (and within the last 90s, with a non-empty current_step) is "live".
            let _liveProcessingAt = null;
            let _liveProcessingClaimId = null;
            for (const cc of claims) {
                if (!cc.processing_at) continue;
                if (!(cc.current_step || '').trim()) continue;
                try {
                    const _ts = new Date(String(cc.processing_at).replace(' UTC', 'Z').replace(' ', 'T')).getTime();
                    if (!isNaN(_ts) && (Date.now() - _ts) < 90000) {
                        if (_liveProcessingAt === null || _ts > _liveProcessingAt) {
                            _liveProcessingAt = _ts;
                            _liveProcessingClaimId = cc.claim_id;
                        }
                    }
                } catch(e) {}
            }
            // Sort claims by readiness so, top→bottom, the user sees:
            //   0. The claim being processed RIGHT NOW (live)
            //   1. Pending — still missing HCFA / IV / Progress note
            //   2. Ready to submit — all docs captured, not yet sent
            //   3. Submitted — done, sinks to the bottom (out of attention)
            // Within each group, newer service dates first.
            const _sortOfficeRe = /\b(9920[1-5]|9921[1-5])\b/;
            const _claimNeedsWork = (c) => {
                if (c.symplisend_submitted) return false;
                const isOffice = !!c.office_visit || _sortOfficeRe.test(String(c.cpt || ''));
                const hcfaOk = !!c.hcfa_s3_path;
                const ivOk = !!c.prog_notes_s3_path;
                const encOk = isOffice || !!c.encounter_file_s3_path;
                return !(hcfaOk && ivOk && encOk);
            };
            const _readinessRank = (c) => {
                if (_liveProcessingClaimId !== null && c.claim_id === _liveProcessingClaimId) return 0;
                if (c.symplisend_submitted) return 3;
                if (_claimNeedsWork(c)) return 1;
                return 2;
            };
            claims.sort((a, b) => {
                const ra = _readinessRank(a);
                const rb = _readinessRank(b);
                if (ra !== rb) return ra - rb;
                return (b.service_date || '').localeCompare(a.service_date || '');
            });
            // If every claim shares one payer, hide the column and surface the
            // payer name once in the section title — saves a wide column for a
            // value that never changes.
            const _uniquePayers = [...new Set(claims.map(c => (c.payer || '').trim()).filter(Boolean))];
            const _payerTitle = document.getElementById('claims-payer-title');
            const _hidePayer = _uniquePayers.length === 1;
            if (_payerTitle) _payerTitle.textContent = _hidePayer ? `· ${_uniquePayers[0]}` : '';
            const _tableWrap = document.querySelector('#claims-section-submissions .claims-table-wrap');
            if (_tableWrap) _tableWrap.classList.toggle('hide-payer', _hidePayer);

            // Type counts — Office Visit vs IV Therapy, plus IVs missing their
            // progress note (encounter_file_s3_path empty) so reviewers see at a
            // glance how many still need a doc.
            const _officeRe = /\b(9920[1-5]|9921[1-5])\b/;
            let _ovCount = 0, _ivCount = 0, _ivNoProgCount = 0;
            for (const c of claims) {
                const isOfc = !!c.office_visit || _officeRe.test(String(c.cpt || ''));
                if (isOfc) {
                    _ovCount++;
                } else {
                    _ivCount++;
                    if (!c.encounter_file_s3_path) _ivNoProgCount++;
                }
            }
            const _typeCounts = document.getElementById('claims-type-counts');
            if (_typeCounts) {
                const missingPart = _ivNoProgCount > 0
                    ? ` <span style="color:var(--text-muted);margin:0 4px;">·</span>` +
                      `<span style="color:#ef4444;font-weight:600;" title="IV Therapy claims with no progress note captured yet">🚫 ${_ivNoProgCount} IV${_ivNoProgCount === 1 ? '' : 's'} without progress note</span>`
                    : '';
                _typeCounts.innerHTML = `<span style="color:#3b82f6;">🏥 ${_ovCount} Office Visit${_ovCount === 1 ? '' : 's'}</span>` +
                                        ` <span style="color:var(--text-muted);margin:0 4px;">·</span>` +
                                        `<span style="color:var(--text-muted);">💉 ${_ivCount} IV Therap${_ivCount === 1 ? 'y' : 'ies'}</span>` +
                                        missingPart;
            }

            body.innerHTML = claims.map(c => {
                const state = parseInt(c.state || c.current_state || 0);
                const stageKey = getStageKey(state);
                // HCFA status.
                //
                // This used to show the chip whenever state >= 2, whether or
                // not the file existed — so a claim whose PDF capture failed
                // still read "📄 HCFA" and looked complete. The subscriber ID
                // is parsed out of HCFA box 1a, so those claims were also
                // missing that, and the pair blocked submission silently.
                //
                // Now: the chip only when the file is really in S3, and a
                // visible failure when generation was attempted without one.
                const hcfaTitle = c.hcfa_generated_at ? `Generated: ${c.hcfa_generated_at}` : '';
                const hcfaTried = !!c.hcfa_triggered || !!c.hcfa_generated_at;
                let hcfaCell;
                if (c.hcfa_s3_path) {
                    hcfaCell = `<span class="hcfa-link" onclick="window.open('/api/hcfa_pdf/${c.claim_id}', '_blank')" title="${hcfaTitle}">📄 HCFA</span>`;
                } else if (hcfaTried) {
                    const when = c.hcfa_generated_at ? ` at ${c.hcfa_generated_at}` : '';
                    hcfaCell = `<span style="color:#ef4444;font-size:11px;font-weight:600;" title="ECW was asked for the HCFA${when} but the PDF never downloaded. Re-run ECW Obtain Claims Documentation for this claim.">🚫 HCFA failed</span>`;
                } else {
                    hcfaCell = '<span class="doc-missing">HCFA —</span>';
                }
                // Submission type from Box 22
                const subType = c.submission_type || (state >= 2 ? 'Pending' : '—');
                const refNo = c.original_ref_no || '';
                const subscriberId = c.subscriber_id || '';
                const subColor = subType === 'First Time Submission' ? '#4ade80' 
                    : subType === 'Resubmission' ? '#f59e0b' : 'var(--text-muted)';
                let subCell;
                if (subType === 'Resubmission' && refNo) {
                    subCell = `<span style="color:${subColor};font-weight:600;font-size:11px;">${subType}</span><br><span style="font-size:10px;color:var(--text-muted);">Ref: ${refNo}</span>`;
                } else if (subType === 'First Time Submission') {
                    subCell = `<span style="color:${subColor};font-weight:600;font-size:11px;">${subType}</span>`
                        + (subscriberId ? `<br><span style="font-size:10px;color:var(--text-muted);">ID: ${subscriberId}</span>` : '');
                } else {
                    subCell = `<span style="color:${subColor};font-weight:500;font-size:11px;">${subType}</span>`;
                }
                // ECW claim-status indicator (post-submission status update in eCW).
                // Green ✓ once the bot set the claim to "Claim sent via Symplisend" and
                // verified it; amber ⏳ while submitted but the ECW status isn't updated yet.
                if (c.ecw_status_updated) {
                    const _ecwTip = 'ECW status set to "' + (c.ecw_status_code || 'Claim sent via Symplisend') + '"'
                        + (c.ecw_status_updated_at ? ' · ' + c.ecw_status_updated_at : '');
                    subCell += `<br><span style="font-size:10px;color:#4ade80;font-weight:600;" title="${_ecwTip}">✓ ECW status updated</span>`;
                } else if (c.symplisend_submitted) {
                    subCell += `<br><span style="font-size:10px;color:var(--warning);" title="Submitted to SympliSend — ECW claim status not updated yet">⏳ ECW status pending</span>`;
                }
                // Progress Notes — clickable if captured
                const hasProgNotes = !!c.prog_notes_s3_path;
                const progNotesCell = hasProgNotes
                    ? `<span class="hcfa-link" onclick="window.open('/api/prog_notes/${c.claim_id}', '_blank')" title="Captured: ${c.prog_notes_captured_at || ''}">📝 IV Note</span>`
                    : '<span class="doc-missing">IV Note —</span>';
                // Office visit (E/M CPT 99201-99205 / 99211-99215) → only HCFA + IV Note,
                // no Progress Note required. Honor the persisted flag or detect from CPT.
                const isOffice = !!c.office_visit || /\b(9920[1-5]|9921[1-5])\b/.test(String(c.cpt || ''));
                const officeVisitCell = isOffice
                    ? `<span style="color:#3b82f6;font-size:11px;font-weight:600;" title="CPT ${c.cpt || ''} — Progress Note not required">🏥 Office Visit</span>`
                    : `<span style="color:var(--text-muted);font-size:11px;">💉 IV Therapy</span>`;
                // Encounter File — clickable if captured; N/A for office visits;
                // "Not Found" (red) when the Encounters tab had no row matching
                // the Rx Start Date + allowed visit type; "Needs Review" (yellow)
                // for any other failure (capture/PDF/click problems where the
                // encounter exists but couldn't be retrieved).
                // Manual-upload affordance for any failed state (Not Found /
                // Needs Review): user clicks "Upload" → file picker → POST PDF
                // to /api/encounter_file/<cid>/upload → DDB cleared + S3 written.
                // "Skip" button flags the claim as not needing a Progress Note
                // (manual override for diagnostic-only / mixed CPT cases).
                const uploadBtn = `<a href="#" onclick="event.stopPropagation();uploadEncounterFile('${c.claim_id}');return false;" style="margin-left:6px;color:var(--info);text-decoration:underline;font-size:10px;">📤 Upload</a>`;
                const skipBtn = `<a href="#" onclick="event.stopPropagation();skipProgressNote('${c.claim_id}', true);return false;" style="margin-left:6px;color:var(--success);text-decoration:underline;font-size:10px;" title="Mark this claim as not needing a Progress Note (submittable without one)">✓ Skip</a>`;
                let encFileCell = '<span class="doc-missing">Progress Note —</span>';
                if (c.encounter_file_s3_path) {
                    encFileCell = `<span class="hcfa-link" onclick="window.open('/api/encounter_file/${c.claim_id}', '_blank')" title="Captured: ${c.encounter_file_captured_at || ''}">📄 Progress Note</span>`;
                } else if (isOffice) {
                    encFileCell = `<span style="color:var(--text-muted);font-size:11px;">N/A · Office Visit</span>`;
                } else if (c.progress_note_not_required) {
                    // Manual override flag set — claim is treated like no-progress-note-needed.
                    encFileCell = `<span style="color:#4ade80;font-size:11px;font-weight:600;" title="Manually flagged: no Progress Note needed">✓ Not Required</span>` +
                                  ` <a href="#" onclick="event.stopPropagation();skipProgressNote('${c.claim_id}', false);return false;" style="margin-left:4px;color:var(--text-muted);text-decoration:underline;font-size:10px;" title="Undo manual override">undo</a>`;
                } else if (c.encounter_capture_failed === 'no_np_or_fu_visit_type'
                           || c.encounter_capture_failed === 'no_rx_start_date') {
                    const reason = c.encounter_capture_failed === 'no_rx_start_date'
                        ? 'No Rx Start Date parsed from IV Note'
                        : 'No encounter on target date with allowed visit type';
                    encFileCell = `<span style="color:#ef4444;font-size:11px;font-weight:600;" title="${reason}">🚫 Not Found</span>${uploadBtn}${skipBtn}`;
                } else if (c.encounter_revision_needed) {
                    encFileCell = `<span style="color:#f59e0b;font-size:11px;font-weight:600;" title="${c.encounter_capture_failed || 'capture failed'}">⚠️ Needs Review</span>${uploadBtn}${skipBtn}`;
                }
                // Currently-processing indicator: only the SINGLE claim with the
                // newest processing_at AND a non-empty current_step lights up. The
                // bot clears current_step at end of each iteration, so the badge
                // disappears as soon as work moves on, not 90s later.
                const _stepLive = (c.current_step || '').trim();
                const isProcessing = (_liveProcessingClaimId !== null
                                      && c.claim_id === _liveProcessingClaimId
                                      && _stepLive.length > 0);
                const rowClass = isProcessing ? 'processing-row' : '';
                const procBadge = isProcessing
                    ? `<br><span class="proc-badge"><span class="proc-spin"></span>${_stepLive}</span>`
                    : '';
                const doc = (inner, title) => `<span class="doc" title="${title || ''}">${inner}</span>`;
                return `<tr class="${rowClass}">
                    <td class="nowrap" style="font-weight:600;color:var(--accent);">${c.claim_id || '—'}<div class="sub">${officeVisitCell}</div>${procBadge}</td>
                    <td class="nowrap">${c.patient_name || '—'}</td>
                    <td class="nowrap">${c.encounter_date || c.service_date || c.dos || '—'}</td>
                    <td class="col-payer nowrap">${(c.payer || '—').replace(' of California', '')}</td>
                    <td class="num nowrap">${c.charges || '—'}</td>
                    <td class="docs-cell"><div class="docs">${doc(hcfaCell, 'HCFA form')}${doc(progNotesCell, 'IV Note')}${doc(encFileCell, 'Progress Note')}</div>${(c.encounter_date || c.prog_note_date || c.iv_note_rx_start_date) && !isOffice ? `<div class="sub">Progress note date ${formatProgNoteDate(c)}</div>` : ''}</td>
                    <td style="min-width:150px">${subCell}</td>
                    <td class="nowrap">${getStagePill(state, c)}</td>
                </tr>`;
            }).join('');

            // Simple per-doc rate indicator. Avg time/claim from deltas between
            // consecutive recent processing_at stamps; drop outliers (<1s, >5min)
            // to ignore stuck claims. This is just informational — not a live
            // backlog countdown.
            const _parseTs = (x) => {
                if (!x) return NaN;
                try { return new Date(String(x).replace(' UTC', 'Z').replace(' ', 'T')).getTime(); }
                catch(e) { return NaN; }
            };
            const _tsAsc = claims
                .map(c => _parseTs(c.processing_at))
                .filter(t => !isNaN(t))
                .sort((a, b) => a - b);
            const _deltas = [];
            for (let i = 1; i < _tsAsc.length; i++) {
                const d = _tsAsc[i] - _tsAsc[i - 1];
                if (d > 1000 && d < 300000) _deltas.push(d);
            }
            const _avgMs = _deltas.length > 0 ? _deltas.reduce((a, b) => a + b, 0) / _deltas.length : null;
            const TYPICAL_UNITS_PER_CLAIM = 2.5;
            const _perUnitSec = _avgMs !== null ? Math.round((_avgMs / TYPICAL_UNITS_PER_CLAIM) / 1000) : null;

            window._etaState = _perUnitSec !== null ? { perUnitSec: _perUnitSec } : null;
            renderEta();
        }

        function renderEta() {
            const etaEl = document.getElementById('claims-eta');
            if (!etaEl) return;
            const s = window._etaState;
            if (!s || s.perUnitSec == null) { etaEl.style.display = 'none'; return; }
            etaEl.style.display = 'inline';
            etaEl.style.color = 'var(--text-muted)';
            etaEl.style.fontWeight = '500';
            etaEl.textContent = `⏱ ~${s.perUnitSec}s/doc avg`;
        }

        function renderTasks(tasks) {
            const list = document.getElementById('tasks-list');
            if (!tasks.length) {
                list.innerHTML = '<div class="empty-state" style="padding:20px;">No open tasks</div>';
                return;
            }
            list.innerHTML = tasks.map(t => `
                <div style="padding:10px;border-bottom:1px solid var(--border);font-size:13px;">
                    <strong style="color:var(--warning);">${t.task_type}</strong> — ${t.claim_id || ''}
                    <div style="color:var(--text-muted);font-size:11px;margin-top:4px;">${t.notes || ''}</div>
                </div>
            `).join('');
        }

        function showToast(msg, type) {
            const toast = document.getElementById('toast');
            toast.textContent = msg;
            toast.className = 'toast ' + type;
            setTimeout(() => { toast.className = 'toast'; }, 3000);
        }

        // ─── BS Claims Drawer ───
        let bsClaimsCache = null;

        async function loadBSClaims() {
            try {
                const res = await fetch('/api/bs-claims');
                bsClaimsCache = await res.json();
            } catch (e) {
                console.error('Failed to load BS claims:', e);
            }
        }

        function openBSDrawer(stageKey, stageName) {
            document.getElementById('bs-overlay').classList.add('active');
            document.getElementById('bs-drawer').classList.add('active');
            document.getElementById('bs-drawer-title').textContent = stageName;

            // Show claims for this stage
            const stage = PIPELINE_STAGES[stageKey];
            if (!stage) return;

            fetch('/api/claims').then(r => r.json()).then(data => {
                const claims = data.claims || [];
                const filtered = claims.filter(c => {
                    const s = parseInt(c.state || c.current_state || 0);
                    return stage.states.includes(s);
                });

                document.getElementById('bs-drawer-meta').innerHTML =
                    `<span>📊 <strong>${filtered.length}</strong> claims in this stage</span>`;

                if (filtered.length === 0) {
                    document.getElementById('bs-drawer-body').innerHTML =
                        `<div class="empty-state">No claims in <strong>${stageName}</strong> yet.</div>`;
                    return;
                }

                document.getElementById('bs-drawer-body').innerHTML = filtered.map(c => {
                    const state = parseInt(c.state || c.current_state || 0);
                    return `<div class="bs-claim-card">
                        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                            <strong style="color:var(--accent);">${c.patient_name || 'Unknown'}</strong>
                            <span class="state-pill documentation" style="font-size:10px;">${STATE_LABELS[state] || 'State ' + state}</span>
                        </div>
                        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:11px;color:var(--text-secondary);">
                            <span>Claim #: <strong style="color:var(--text-primary);">${c.claim_id}</strong></span>
                            <span>DOS: ${c.service_date || '—'}</span>
                            <span>DOS: <strong>${c.encounter_date || '—'}</strong></span>
                            <span>Charges: <strong style="color:var(--warning);">${c.charges || '—'}</strong></span>
                            <span>Subscriber: ${c.subscriber_id || '—'}</span>
                            <span>Payer: ${c.payer || '—'}</span>
                        </div>
                    </div>`;
                }).join('');
            });
        }

        function closeBSDrawer() {
            document.getElementById('bs-overlay').classList.remove('active');
            document.getElementById('bs-drawer').classList.remove('active');
        }

        // ─── HCFA Viewer ───
        let hcfaCurrentClaimId = null;
        let hcfaCurrentView = 'popup'; // 'popup' or 'after_click'

        async function openHCFAViewer(claimId, patientName) {
            hcfaCurrentClaimId = claimId;
            hcfaCurrentView = 'popup';

            document.getElementById('hcfa-lightbox').classList.add('active');
            document.getElementById('hcfa-lightbox-title').textContent =
                `HCFA — Claim ${claimId} (${patientName})`;

            // Build tabs
            const tabsEl = document.getElementById('hcfa-tabs');
            tabsEl.innerHTML = `
                <button class="hcfa-tab active" data-view="popup" onclick="switchHCFATab('popup', this)">📋 Claim Popup</button>
                <button class="hcfa-tab" data-view="after_click" onclick="switchHCFATab('after_click', this)">📄 After HCFA Click</button>
            `;

            loadHCFAImage(claimId, 'popup');
        }

        function switchHCFATab(view, tabEl) {
            // Update tab styles
            document.querySelectorAll('.hcfa-tab').forEach(t => t.classList.remove('active'));
            tabEl.classList.add('active');
            hcfaCurrentView = view;
            loadHCFAImage(hcfaCurrentClaimId, view);
        }

        function loadHCFAImage(claimId, view) {
            const body = document.getElementById('hcfa-lightbox-body');
            const imgUrl = `/api/hcfa/${claimId}/${view}?t=${Date.now()}`;
            body.innerHTML = `<div class="hcfa-placeholder">Loading screenshot...</div>`;

            const img = new Image();
            img.onload = () => {
                body.innerHTML = '';
                body.appendChild(img);
            };
            img.onerror = () => {
                body.innerHTML = `<div class="hcfa-placeholder">
                    <div style="font-size:48px;margin-bottom:16px;">📭</div>
                    <div style="font-size:14px;">No screenshot available for this view.</div>
                    <div style="font-size:12px;margin-top:8px;color:var(--text-muted);">Run the HCFA generation task to capture screenshots.</div>
                </div>`;
            };
            img.src = imgUrl;
        }

        function closeHCFAViewer() {
            document.getElementById('hcfa-lightbox').classList.remove('active');
            hcfaCurrentClaimId = null;
        }

        // Close on Escape
        document.addEventListener('keydown', e => {
            if (e.key === 'Escape') {
                closeHCFAViewer();
                closeBSDrawer();
            }
        });

        // Stop Agent
        async function stopAgent() {
            if (!confirm('⛔ Stop the agent? This will kill any running browser, purge the SQS queue, and stop the service.')) return;
            const btn = document.getElementById('stopBtn');
            btn.disabled = true;
            btn.classList.add('stopping');
            btn.innerHTML = '⏳ Stopping...';
            try {
                const resp = await fetch('/api/stop-agent?bot=' + encodeURIComponent(window.activeBot || 'submissions'), { method: 'POST' });
                const d = await resp.json();
                if (d.success) {
                    btn.innerHTML = '✅ Stopped';
                    btn.style.borderColor = 'var(--success)';
                    btn.style.background = 'rgba(16,185,129,0.15)';
                    setTimeout(() => {
                        btn.innerHTML = '⛔ Stop Agent';
                        btn.disabled = false;
                        btn.classList.remove('stopping');
                        btn.style.borderColor = '';
                        btn.style.background = '';
                        loadLogs();
                    }, 3000);
                } else {
                    btn.innerHTML = '❌ Error';
                    alert('Stop failed: ' + (d.error || 'Unknown'));
                    setTimeout(() => {
                        btn.innerHTML = '⛔ Stop Agent';
                        btn.disabled = false;
                        btn.classList.remove('stopping');
                    }, 2000);
                }
            } catch (e) {
                btn.innerHTML = '❌ Error';
                alert('Stop failed: ' + e);
                setTimeout(() => {
                    btn.innerHTML = '⛔ Stop Agent';
                    btn.disabled = false;
                    btn.classList.remove('stopping');
                }, 2000);
            }
        }

        // ── Fix Coding IVs saved reports ──
        function _fmtBytes(b) {
            if (b < 1024) return b + ' B';
            if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
            return (b / 1048576).toFixed(1) + ' MB';
        }
        function _fmtRunId(rid) {
            // YYYYMMDD_HHMMSS → YYYY-MM-DD HH:MM:SS
            const m = rid.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/);
            if (!m) return rid;
            return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`;
        }
        

        

        // Auto-refresh
        updateTaskTemplate();
        loadData();
        loadLogs();
        loadBSClaims();
        // The headline counter polls a counts-only endpoint so it keeps up with
        // the bot; the full claims table costs ~3MB a call, so it refreshes on
        // a slower beat.
        setInterval(loadCounts, 4000);
        setInterval(loadData, 15000);
        // The checks table is small; keep it live while a run is going.
        setInterval(() => { if (window.activeBot === 'eob') loadChecks(); }, 15000);
        setInterval(() => { if (window.activeBot === 'eob') loadFolders(); }, 60000);

        // ---- SharePoint folders (Remittance) ----
        // The folder tree exactly as the bot walked it, with the check
        // numbers it read in each folder — so a person can open
        // Posted Checks → 2026 → 01'2026 → 01-06-2026 and compare with SharePoint.
        function toggleFolders() {
            const b = document.getElementById('folders-body'), t = document.getElementById('folders-toggle');
            if (!b) return;
            b.hidden = !b.hidden;
            if (t) t.textContent = b.hidden ? 'Show' : 'Hide';
            if (!b.hidden && typeof loadFolders === 'function') loadFolders();
        }
        function foldersOpenAll(open) { document.querySelectorAll('#folders-tree details').forEach(d => d.open = open); }
        async function loadFolders() {
            try {
                const res = await fetch('/api/checks/folders');
                window._foldersData = await res.json();
                renderFolders();
            } catch (e) { console.error('loadFolders failed', e); }
        }
        function renderFolders() {
            const el = document.getElementById('folders-tree');
            const meta = document.getElementById('folders-meta');
            const data = window._foldersData;
            if (!el || !data) return;
            const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
            const q = (document.getElementById('folders-search')?.value || '').trim().toLowerCase();
            // Highlight the search hit without a regex (no escaping to get wrong).
            const hi = s => { const str = String(s ?? ''); if (!q) return esc(str); const at = str.toLowerCase().indexOf(q);
                return at < 0 ? esc(str) : esc(str.slice(0, at)) + '<mark>' + esc(str.slice(at, at + q.length)) + '</mark>' + esc(str.slice(at + q.length)); };
            const hit = f => !q || (f.checks || []).some(c => String(c.check_number).includes(q) || String(c.file).toLowerCase().includes(q))
                                  || (f.unreadable || []).some(u => String(u.file).toLowerCase().includes(q))
                                  || Object.values(f.children || {}).some(hit);
            const node = (name, f, depth) => {
                if (!hit(f)) return '';
                const kids = Object.keys(f.children || {}).sort().map(k => node(k, f.children[k], depth + 1)).join('');
                const rows = (f.checks || []).filter(c => !q || String(c.check_number).includes(q) || String(c.file).toLowerCase().includes(q) || Object.values(f.children || {}).length === 0 && hit(f))
                    .sort((a, b) => String(a.file).localeCompare(String(b.file)));
                const unread = (f.unreadable || []).filter(u => !q || String(u.file).toLowerCase().includes(q));
                const table = (rows.length || unread.length) ? `<table><tbody>${rows.map(c => `
                    <tr><td><strong>${hi(c.check_number)}</strong></td><td class="num">${c.amount ? '$' + esc(c.amount) : '<span class="unread">no amount</span>'}</td>
                        <td>${c.url ? `<a href="${esc(c.url)}" target="_blank">${hi(c.file)}</a>` : hi(c.file)}</td>
                        <td style="color:var(--text-muted)">${esc(c.read_by || '')}${c.payer ? ' · ' + esc(c.payer) : ''}</td></tr>`).join('')}${unread.map(u => `
                    <tr><td class="unread">not read</td><td></td><td>${u.url ? `<a href="${esc(u.url)}" target="_blank">${hi(u.file)}</a>` : hi(u.file)}</td><td class="unread" style="font-size:11px">${esc(u.problem || '')}</td></tr>`).join('')}</tbody></table>` : '';
                return `<details${q || depth === 0 ? ' open' : ''}><summary><span class="fname">${esc(name)}</span>
                    <span class="fcount"><b>${f.total_checks}</b> check${f.total_checks === 1 ? '' : 's'} · ${f.total_files} file${f.total_files === 1 ? '' : 's'}${f.total_unreadable ? ` · <span class="unread">${f.total_unreadable} not read</span>` : ''}</span></summary>${table}${kids}</details>`;
            };
            const roots = Object.keys(data.tree || {}).sort();
            el.innerHTML = roots.length ? roots.map(k => node(k, data.tree[k], 0)).join('') : '<div class="empty-state">No folder has been read yet.</div>';
            if (meta) meta.textContent = `${data.total_checks} checks read from ${data.total_files} files in ${data.folders} folders` + (data.total_unreadable ? ` · ${data.total_unreadable} files not read` : '') + (data.skipped && data.skipped.length ? ` · left out: ${data.skipped.join(', ')}` : '');
        }
        setInterval(loadLogs, 5000);
        setInterval(loadBSClaims, 30000);
    </script>
</body>
</html>
"""


@app.route('/')
def dashboard():
    return render_template_string(DASHBOARD_HTML,
        state_labels=STATE_LABELS,
        pipeline_stages=PIPELINE_STAGES,
        bot_novnc={k: v['novnc_port'] for k, v in BOT_ROUTING.items()},
        bot_names={k: v['name'] for k, v in BOT_ROUTING.items()})


@app.route('/client')
def client_dashboard():
    """Client-facing read-only dashboard showing claims submission status."""
    template_path = os.path.join(os.path.dirname(__file__), 'templates', 'client.html')
    with open(template_path, 'r') as f:
        html = f.read()
    return html


@app.route('/api/claims')
def api_claims():
    try:
        claims_table = dynamodb.Table('helixona-claims')
        tasks_table = dynamodb.Table('helixona-tasks')

        claims = scan_all(claims_table)
        tasks = [t for t in scan_all(tasks_table) if t.get('status') == 'Open']

        # Convert Decimal to int/float for JSON
        for c in claims:
            for k, v in c.items():
                if hasattr(v, 'real'):
                    c[k] = int(v)

        return jsonify({'claims': claims, 'tasks': tasks})
    except Exception as e:
        return jsonify({'claims': [], 'tasks': [], 'error': str(e)})


@app.after_request
def _gzip_large_responses(resp):
    """Compress big JSON and HTML on the way out.

    /api/claims is built in 0.6 s on the server and is 3.2 MB of JSON, which
    took ~50 s to cross the network uncompressed — longer than the 15 s poll,
    so the table was always a refresh behind and a tab switch kept showing the
    previous tab's claims. JSON of this shape compresses about tenfold.
    """
    try:
        if (resp.status_code == 200
                and resp.mimetype in ('application/json', 'text/html')
                and 'gzip' in request.headers.get('Accept-Encoding', '').lower()
                and 'Content-Encoding' not in resp.headers
                and not resp.direct_passthrough):
            data = resp.get_data()
            if len(data) > 2048:
                import gzip
                resp.set_data(gzip.compress(data, compresslevel=5))
                resp.headers['Content-Encoding'] = 'gzip'
                resp.headers['Vary'] = 'Accept-Encoding'
    except Exception:
        pass
    return resp


@app.route('/api/mfa-code', methods=['POST'])
def api_mfa_code():
    """Hand a Blue Shield 2-step code to whichever bot is waiting for one."""
    try:
        from src.utils.mfa_relay import post_manual_code
        payload = request.get_json(silent=True) or {}
        item = post_manual_code(dynamodb, payload.get('code', ''))
        return jsonify({'success': True, 'submitted_at': item['submitted_at']})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _folder_tree(items):
    """The SharePoint folder tree as the bot read it: every file it looked
    at, under the folder it was in, with the check number it read (or why it
    could not). Built from helixona-checks: one row per check read (its last
    file) plus one 'unreadable:<file>' row per file that gave no check."""
    def new():
        return {'children': {}, 'checks': [], 'unreadable': [], 'total_files': 0, 'total_checks': 0, 'total_unreadable': 0}
    tree = {}
    files = set()
    for it in items:
        path = str(it.get('copy_file') or '')
        if not path:
            continue
        files.add(path)
        parts = path.split('/')
        folders, name = parts[:-1], parts[-1]
        node_path = []
        cur = tree
        for f in folders:
            cur = cur.setdefault(f, new())
            node_path.append(cur)
            cur = cur['children']
        leaf = node_path[-1] if node_path else tree.setdefault('(root)', new())
        key = str(it.get('check_number', ''))
        if key.startswith('deposit:'):
            nums = [str(c) for c in (it.get('deposit_checks') or [])]
            leaf['checks'].append({'check_number': f"🏦 deposit of {len(nums)} checks · ${it.get('deposit_total') or '?'}: " + ', '.join(nums),
                                   'amount': str(it.get('deposit_total') or ''), 'file': name, 'url': it.get('copy_url', ''),
                                   'read_by': 'deposit slip', 'payer': '', 'verdict': ''})
            for n in node_path or [leaf]:
                n['total_files'] += 1
                n['total_checks'] += len(nums)
            continue
        if it.get('deposit_file'):
            continue   # its check rows hang off the deposit's file
        if key.startswith('unreadable:') or not it.get('has_copy'):
            leaf['unreadable'].append({'file': name, 'url': it.get('copy_url', ''), 'problem': str(it.get('copy_problem') or '')})
            for n in node_path or [leaf]:
                n['total_files'] += 1
                n['total_unreadable'] += 1
        else:
            leaf['checks'].append({'check_number': key, 'amount': str(it.get('copy_amount') or ''), 'file': name,
                                   'url': it.get('copy_url', ''), 'read_by': str(it.get('copy_read_by') or ''),
                                   'payer': str(it.get('copy_payer') or ''), 'verdict': str(it.get('verdict') or '')})
            for n in node_path or [leaf]:
                n['total_files'] += 1
                n['total_checks'] += 1

    def count(nodes):
        return sum(1 + count(n['children']) for n in nodes.values())
    return {'tree': tree, 'folders': count(tree), 'total_files': len(files),
            'total_checks': sum(n['total_checks'] for n in tree.values()),
            'total_unreadable': sum(n['total_unreadable'] for n in tree.values())}


@app.route('/api/checks/folders')
def api_checks_folders():
    """What was collected from SharePoint, folder by folder."""
    try:
        items = scan_all(dynamodb.Table('helixona-checks'))
        run = next((it for it in items if it.get('check_number') == '_run'), {})
        out = _folder_tree([it for it in items if not str(it.get('check_number', '')).startswith('_')])
        out['skipped'] = ['Insurance Check Tracker', 'Posted Checks/2025']
        out['last_run'] = str(run.get('updated_at') or '')
        return jsonify(json.loads(json.dumps(out, default=str)))
    except Exception as e:
        return jsonify({'error': str(e), 'tree': {}, 'folders': 0, 'total_files': 0, 'total_checks': 0, 'total_unreadable': 0})


def _checks_rows():
    """The check rows, the counts over them (so the tiles always agree with
    the table, whatever the last run looked at), and the last run's trace."""
    from src.checks.reconcile import summarize
    table = dynamodb.Table('helixona-checks')
    items = scan_all(table)
    meta = next((it for it in items if it.get('check_number') == '_summary'), {})
    run = next((it for it in items if it.get('check_number') == '_run'), {})
    # Files the reader could not make a check out of live under
    # 'unreadable:<file>' and are not checks; they are counted separately.
    rows = [it for it in items if not str(it.get('check_number', '')).startswith(('_', 'unreadable:', 'deposit:'))]
    deposits = [it for it in items if str(it.get('check_number', '')).startswith('deposit:')]
    unreadable = [it for it in items if str(it.get('check_number', '')).startswith('unreadable:')]
    for r in rows:
        r['flags'] = list(r.get('flags') or [])
        # The number as printed on the check (zeros kept) when a copy was read; else the bare key.
        raw = str(r.get('copy_check_raw') or '')
        r['check_full'] = raw if raw.endswith(str(r['check_number'])) else str(r['check_number'])
    rows.sort(key=lambda r: (str(r.get('bs_date') or r.get('copy_date') or ''), str(r.get('check_number'))), reverse=True)
    # A deposit: the slip's total and the checks filed under it, each its own row.
    by_dep = {}
    for r in rows:
        if r.get('deposit_file'):
            by_dep.setdefault(str(r['deposit_file']), []).append(r)
    dep_rows = []
    for d in deposits:
        members = by_dep.get(str(d.get('copy_file') or ''), [])
        dep_rows.append({'file': str(d.get('copy_file') or ''), 'folder': str(d.get('copy_folder') or ''),
                         'url': str(d.get('copy_url') or ''), 'date': str(d.get('deposit_date') or ''),
                         'total': str(d.get('deposit_total') or ''), 'count': int(d.get('deposit_count') or len(members)),
                         'checks': [str(r['check_number']) for r in members],
                         'posted': sum(r.get('verdict') == 'posted' for r in members),
                         'attention': sum(r.get('verdict') in ('not in eCW', 'unposted') or 'amounts differ' in r['flags'] for r in members)})
    summary = {**summarize(rows), 'unreadable': len(unreadable), 'deposits': dep_rows,
               'unreadable_files': [{'file': str(u.get('copy_file', '')), 'problem': str(u.get('copy_problem', ''))} for u in unreadable[:50]],
               **{k: meta[k] for k in ('since', 'reconciled_at', 'ecw_checked', 'run_rows') if k in meta},
               'last_run': {k: v for k, v in run.items() if k != 'check_number'}}
    return rows, summary


@app.route('/checks')
def checks_client_view():
    """The billing team's page: one screen, the checks that do not match and
    what to do about each. No bot controls, no logs. dashboard_checks.html
    next to this file; it reads /api/checks."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dashboard_checks.html')
    with open(path, encoding='utf-8') as fh:
        return fh.read()


@app.route('/api/checks')
def api_checks():
    """The check reconciliation: one row per check number, and the summary."""
    try:
        rows, summary = _checks_rows()
        resp = jsonify(json.loads(json.dumps({'rows': rows, 'summary': summary}, default=str)))
        resp.headers['Cache-Control'] = 'no-store'   # the team page polls this; never a stale copy
        return resp
    except Exception as e:
        return jsonify({'error': str(e), 'rows': [], 'summary': {}})


@app.route('/api/checks.csv')
def api_checks_csv():
    import csv
    import io
    from flask import Response
    cols = ['check_number', 'check_full', 'deposit_file', 'deposit_total', 'copy_page', 'verdict', 'flags', 'has_copy', 'copy_amount', 'copy_file', 'copy_url', 'in_blue_shield',
            'bs_amount', 'bs_status', 'bs_date', 'cashed_date', 'in_ecw', 'ecw_payment_id', 'ecw_amount',
            'ecw_posted', 'ecw_unposted', 'reconciled_at']
    try:
        rows, _summary = _checks_rows()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([' | '.join(r['flags']) if c == 'flags' else str(r.get(c, '')) for c in cols])
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename="checks.csv"'})


@app.route('/api/claim-counts')
def api_claim_counts():
    """Just the hero numbers, per bot.

    /api/claims returns every field of every claim — around 3MB and 2-3
    seconds once the table passed a thousand rows. Polling that every 4s to
    move one counter meant the number lagged behind the bot that was updating
    it. This projects only what the counter needs, so the headline can refresh
    quickly while the full table refreshes on its own slower schedule.
    """
    try:
        table = dynamodb.Table('helixona-claims')
        items, kwargs = [], {
            'ProjectionExpression': '#st, submission_type, symplisend_submitted, eob_check_eft',
            'ExpressionAttributeNames': {'#st': 'state'},
        }
        while True:
            resp = table.scan(**kwargs)
            items.extend(resp.get('Items', []))
            if 'LastEvaluatedKey' not in resp:
                break
            kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']

        submitted_states = set(PIPELINE_STAGES['submitted']['states'])

        def tally(rows):
            total = len(rows)
            done = sum(1 for r in rows
                       if int(r.get('state') or 0) in submitted_states)
            return {'submitted': done, 'total': total}

        is_resub = lambda r: 'resub' in str(r.get('submission_type', '')).lower()
        # Remittance's headline is not a slice of the submissions/resubmissions
        # partition: of the checks compared, how many the three sources agree on.
        try:
            check_rows, _sm = _checks_rows()
        except Exception:
            check_rows = []
        matching = [r for r in check_rows if r.get('verdict') == 'posted' and not r.get('flags')]
        return jsonify({
            'submissions': tally([r for r in items if not is_resub(r)]),
            'resubmissions': tally([r for r in items if is_resub(r)]),
            'eob': {'submitted': len(matching), 'total': len(check_rows)},
            'all': tally(items),
        })
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/logs')
def api_logs():
    """Live agent log tail.

    Query params:
      n      : max line count to return (default 50, capped at 5000).
      level  : 'problems' → only WARNING / ERROR / CRITICAL / Traceback /
                Exception / ❌ / ⛔ / ⚠️ lines (great for triage).
      since  : journalctl --since value (e.g. '2 hours ago'). Default '30 min ago'.
    """
    try:
        n = int(request.args.get('n', 50))
        n = max(1, min(n, 5000))
        level = request.args.get('level', '')
        since = request.args.get('since', '30 min ago')
        service = BOT_ROUTING[_bot_from_request()]['service']
        cmd = ['journalctl', '-u', service, '--since', since, '--no-pager', '--output=short']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        lines = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
        cleaned = []
        for line in lines:
            if 'python[' in line:
                parts = line.split('python[')
                if len(parts) > 1:
                    cleaned.append(parts[1].split(']: ', 1)[-1] if ']: ' in parts[1] else parts[1])
                else:
                    cleaned.append(line)
            else:
                cleaned.append(line)
        if level == 'problems':
            markers = ('WARNING', 'ERROR', 'CRITICAL', 'Traceback', 'Exception',
                       '❌', '⛔', '⚠️')
            cleaned = [l for l in cleaned if any(m in l for m in markers)]
        return jsonify({'logs': cleaned[-n:], 'total': len(cleaned)})
    except Exception as e:
        return jsonify({'logs': [f'Error fetching logs: {str(e)}']})


@app.route('/api/send-task', methods=['POST'])
def api_send_task():
    try:
        payload = request.get_json() or {}
        bot = (payload.pop('bot', None) or 'submissions')
        if bot not in BOT_ROUTING:
            bot = 'submissions'
        routing = BOT_ROUTING[bot]
        if not routing['queue_url']:
            return jsonify({'success': False, 'error': f'No queue URL configured for bot={bot}'})
        sqs.send_message(
            QueueUrl=routing['queue_url'],
            MessageBody=json.dumps(payload)
        )
        # Ensure the right agent service is running (in case it was stopped)
        subprocess.run(
            ['sudo', 'systemctl', 'start', routing['service']],
            capture_output=True, timeout=10
        )
        return jsonify({'success': True, 'bot': bot})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/bs-claims')
def api_bs_claims():
    """Serve scraped Blue Shield claims data from the agent."""
    try:
        bs_file = '/opt/helixona-agent/bs_claims.json'
        if os.path.exists(bs_file):
            with open(bs_file) as f:
                data = json.load(f)
            return jsonify(data)
        else:
            return jsonify({'raw_claims': [], 'total_found': 0, 'scraped_at': None})
    except Exception as e:
        return jsonify({'error': str(e), 'raw_claims': [], 'total_found': 0})


@app.route('/api/hcfa_pdf/<claim_id>')
def api_hcfa_pdf(claim_id):
    """Serve HCFA PDF for a claim from /tmp or S3."""
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', claim_id):
        return jsonify({'error': 'Invalid claim ID'}), 400

    local_path = f'/tmp/hcfa_{claim_id}.pdf'
    if os.path.exists(local_path) and os.path.getsize(local_path) > 500:
        return send_file(local_path, mimetype='application/pdf',
                         download_name=f'hcfa_{claim_id}.pdf')

    try:
        import boto3
        s3 = boto3.client('s3', region_name='us-west-2')
        bucket = 'helixona-claims-docs-eb2f8e3c'
        s3_key = f'hcfa_forms/{claim_id}_hcfa.pdf'
        local_dl = f'/tmp/hcfa_{claim_id}_dl.pdf'
        s3.download_file(bucket, s3_key, local_dl)
        return send_file(local_dl, mimetype='application/pdf',
                         download_name=f'hcfa_{claim_id}.pdf')
    except Exception as e:
        return jsonify({'error': f'HCFA PDF not found: {str(e)}'}), 404


@app.route('/api/hcfa/<claim_id>/<view_type>')
def api_hcfa_screenshot(claim_id, view_type):
    """Serve HCFA screenshots from /tmp on the EC2.
    
    view_type: 'popup' → /tmp/hcfa_popup_{claim_id}.png
               'after_click' → /tmp/hcfa_after_click_{claim_id}.png
    """
    import re
    # Sanitize claim_id to prevent path traversal
    if not re.match(r'^[a-zA-Z0-9_-]+$', claim_id):
        return jsonify({'error': 'Invalid claim ID'}), 400

    file_map = {
        'popup': f'/tmp/hcfa_popup_{claim_id}.png',
        'after_click': f'/tmp/hcfa_after_click_{claim_id}.png',
    }

    file_path = file_map.get(view_type)
    if not file_path or not os.path.exists(file_path):
        return jsonify({'error': 'Screenshot not found'}), 404

    return send_file(file_path, mimetype='image/png')


@app.route('/api/prog_notes/<claim_id>')
def api_prog_notes_pdf(claim_id):
    """Serve Progress Notes PDF for a claim.
    
    First check /tmp for the local file, then fall back to S3.
    """
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', claim_id):
        return jsonify({'error': 'Invalid claim ID'}), 400

    # Check local /tmp first
    local_path = f'/tmp/prog_notes_{claim_id}.pdf'
    if os.path.exists(local_path) and os.path.getsize(local_path) > 500:
        return send_file(local_path, mimetype='application/pdf',
                         download_name=f'prog_notes_{claim_id}.pdf')

    # Fall back to S3
    try:
        import boto3
        s3 = boto3.client('s3', region_name='us-west-2')
        bucket = 'helixona-claims-docs-eb2f8e3c'
        s3_key = f'prog_notes/{claim_id}_prog_notes.pdf'
        local_dl = f'/tmp/prog_notes_{claim_id}_dl.pdf'
        s3.download_file(bucket, s3_key, local_dl)
        return send_file(local_dl, mimetype='application/pdf',
                         download_name=f'prog_notes_{claim_id}.pdf')
    except Exception as e:
        return jsonify({'error': f'Progress Notes not found: {str(e)}'}), 404


@app.route('/api/encounter_file/<claim_id>')
@app.route('/api/encounter_file/<claim_id>/<int:idx>')
def api_encounter_file(claim_id, idx=None):
    """Serve Encounter File PDF for a claim.

    The agent uploads each matching encounter as a numbered file:
      encounter_files/{claim_id}_encounter_{n}.pdf
    DynamoDB stores the first one in `encounter_file_s3_path` and the full list
    in `encounter_files_s3_paths`. With no idx, returns the first; with idx=N
    (1-based), returns the N-th captured encounter for that claim.
    """
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', claim_id):
        return jsonify({'error': 'Invalid claim ID'}), 400

    # Try local /tmp first — agent writes encounter_file_{cid}_{n}.pdf
    nth = idx if idx else 1
    for local_candidate in [
        f'/tmp/encounter_file_{claim_id}_{nth}.pdf',
        f'/tmp/encounter_file_{claim_id}.pdf',  # legacy single-file path
    ]:
        if os.path.exists(local_candidate) and os.path.getsize(local_candidate) > 500:
            return send_file(local_candidate, mimetype='application/pdf',
                             download_name=f'encounter_{claim_id}.pdf')

    # Read the actual S3 path from DynamoDB
    s3_uri = None
    try:
        item = dynamodb.Table('helixona-claims').get_item(Key={'claim_id': claim_id}).get('Item', {})
        if idx and item.get('encounter_files_s3_paths'):
            paths = item['encounter_files_s3_paths']
            if 1 <= idx <= len(paths):
                s3_uri = paths[idx - 1]
        if not s3_uri:
            s3_uri = item.get('encounter_file_s3_path')
    except Exception as ddb_err:
        return jsonify({'error': f'DynamoDB lookup failed: {ddb_err}'}), 500

    # Fall back to the legacy hardcoded key if DDB has no record
    if not s3_uri:
        s3_uri = f's3://helixona-claims-docs-eb2f8e3c/encounter_files/{claim_id}_encounter_1.pdf'

    try:
        import boto3
        s3 = boto3.client('s3', region_name='us-west-2')
        # Parse s3://bucket/key into bucket + key
        if s3_uri.startswith('s3://'):
            without_scheme = s3_uri[5:]
            bucket, s3_key = without_scheme.split('/', 1)
        else:
            bucket = 'helixona-claims-docs-eb2f8e3c'
            s3_key = s3_uri
        local_dl = f'/tmp/encounter_file_{claim_id}_{nth}_dl.pdf'
        s3.download_file(bucket, s3_key, local_dl)
        return send_file(local_dl, mimetype='application/pdf',
                         download_name=f'encounter_{claim_id}.pdf')
    except Exception as e:
        return jsonify({'error': f'Encounter file not found: {str(e)}'}), 404


@app.route('/api/encounter_files/<claim_id>/list')
def api_encounter_files_list(claim_id):
    """List all encounter file URLs + visit types for a claim (from DynamoDB)."""
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', claim_id):
        return jsonify({'error': 'Invalid claim ID'}), 400
    try:
        item = dynamodb.Table('helixona-claims').get_item(Key={'claim_id': claim_id}).get('Item', {})
        paths = item.get('encounter_files_s3_paths', [])
        visit_types = item.get('encounter_files_visit_types', [])
        files = []
        for i, p in enumerate(paths, start=1):
            files.append({
                'idx': i,
                'visit_type': visit_types[i - 1] if i - 1 < len(visit_types) else '',
                'url': f'/api/encounter_file/{claim_id}/{i}',
                's3_path': p,
            })
        return jsonify({'claim_id': claim_id, 'count': len(files), 'files': files})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/encounter_file/<claim_id>/upload', methods=['POST'])
def api_encounter_file_upload(claim_id):
    """Manual upload of a Progress Note PDF for a claim the bot couldn't capture.
    Stores it at the same S3 key the bot would write (encounter_files/{cid}_encounter_1.pdf),
    updates DynamoDB so the claim is no longer flagged Not Found / Needs Review.
    """
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', claim_id):
        return jsonify({'error': 'Invalid claim ID'}), 400
    if 'file' not in request.files:
        return jsonify({'error': 'No file in request'}), 400
    upload = request.files['file']
    if not upload.filename or not upload.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'File must be a PDF'}), 400
    # Save to /tmp, then upload to S3
    local_path = f'/tmp/manual_upload_{claim_id}.pdf'
    try:
        upload.save(local_path)
        size = os.path.getsize(local_path)
        if size < 500:
            return jsonify({'error': f'File too small ({size} bytes)'}), 400
        import boto3
        s3 = boto3.client('s3', region_name='us-west-2')
        s3_key = f'encounter_files/{claim_id}_encounter_1.pdf'
        bucket = 'helixona-claims-docs-eb2f8e3c'
        s3.upload_file(local_path, bucket, s3_key)
        s3_uri = f's3://{bucket}/{s3_key}'
        # Update DynamoDB — clear Not Found / Needs Review state, mark manual upload.
        import time as _t
        now = _t.strftime('%Y-%m-%d %H:%M:%S UTC', _t.gmtime())
        dynamodb.Table('helixona-claims').update_item(
            Key={'claim_id': claim_id},
            UpdateExpression=(
                'SET encounter_file_s3_path = :p, encounter_files_s3_paths = :pl, '
                'encounter_files_count = :one, encounter_files_visit_types = :vt, '
                'encounter_file_captured_at = :now, encounter_revision_needed = :f, '
                'encounter_date_mismatch = :f, encounter_pick_strategy = :ms '
                'REMOVE encounter_capture_failed, encounter_capture_failed_at'
            ),
            ExpressionAttributeValues={
                ':p': s3_uri, ':pl': [s3_uri], ':one': 1, ':vt': ['MANUAL'],
                ':now': now, ':f': False, ':ms': 'manual_upload',
            },
        )
        try: os.remove(local_path)
        except Exception: pass
        return jsonify({'ok': True, 's3_path': s3_uri, 'size': size})
    except Exception as e:
        try: os.remove(local_path)
        except Exception: pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/claim/<claim_id>/skip_progress_note', methods=['POST'])
def api_skip_progress_note(claim_id):
    """Manual override: flag this claim as not requiring a Progress Note so it
    becomes submittable without one. Used for claims like diagnostic-only or
    other CPT codes the reviewer knows don't need an encounter file.
    Body: {"skip": true|false}. true sets the flag, false removes it.
    """
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', claim_id):
        return jsonify({'error': 'Invalid claim ID'}), 400
    body = request.get_json(silent=True) or {}
    skip = bool(body.get('skip'))
    try:
        if skip:
            # Set flag + clear the Needs Review state so the claim is submittable.
            dynamodb.Table('helixona-claims').update_item(
                Key={'claim_id': claim_id},
                UpdateExpression=(
                    'SET progress_note_not_required = :t, '
                    'encounter_revision_needed = :f '
                    'REMOVE encounter_capture_failed, encounter_capture_failed_at'
                ),
                ExpressionAttributeValues={':t': True, ':f': False},
            )
        else:
            dynamodb.Table('helixona-claims').update_item(
                Key={'claim_id': claim_id},
                UpdateExpression='REMOVE progress_note_not_required',
            )
        return jsonify({'ok': True, 'skip': skip})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/claim/<claim_id>/prog_note_date', methods=['POST'])
def api_edit_prog_note_date(claim_id):
    """Manually set the Progress Note Date (iv_note_rx_start_date) for a claim.
    Accepts JSON {"date": "MM/DD/YYYY"} or {"date": "YYYY-MM-DD"} or empty string to clear.
    Stored canonically as YYYY-MM-DD in DDB to match what the bot writes.
    """
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', claim_id):
        return jsonify({'error': 'Invalid claim ID'}), 400
    body = request.get_json(silent=True) or {}
    raw = (body.get('date') or '').strip()
    # Empty string → remove the field entirely.
    if not raw:
        try:
            dynamodb.Table('helixona-claims').update_item(
                Key={'claim_id': claim_id},
                UpdateExpression='REMOVE iv_note_rx_start_date',
            )
            return jsonify({'ok': True, 'cleared': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    # Normalize: accept MM/DD/YYYY or YYYY-MM-DD
    m_us = re.match(r'^(\d{2})/(\d{2})/(\d{4})$', raw)
    m_iso = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', raw)
    if m_us:
        mm, dd, yyyy = m_us.groups()
        iso = f'{yyyy}-{mm}-{dd}'
    elif m_iso:
        iso = raw
    else:
        return jsonify({'error': 'Date must be MM/DD/YYYY or YYYY-MM-DD'}), 400
    try:
        dynamodb.Table('helixona-claims').update_item(
            Key={'claim_id': claim_id},
            UpdateExpression='SET iv_note_rx_start_date = :d',
            ExpressionAttributeValues={':d': iso},
        )
        return jsonify({'ok': True, 'date': iso})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stop-agent', methods=['POST'])
def api_stop_agent():
    """Emergency stop — kills browser processes and stops the agent."""
    try:
        # Kill any running chrome/chromium browsers spawned by the agent
        subprocess.run(
            ['pkill', '-f', 'chromium.*helixona'],
            capture_output=True, timeout=5
        )
        subprocess.run(
            ['pkill', '-f', 'chrome.*user-data-dir.*browser-profile'],
            capture_output=True, timeout=5
        )
        bot = _bot_from_request()
        routing = BOT_ROUTING[bot]
        # Purge the bot's SQS queue to prevent re-processing the same task
        if routing['queue_url']:
            try:
                sqs.purge_queue(QueueUrl=routing['queue_url'])
            except Exception:
                pass
        # STOP the bot's agent service (not restart)
        result = subprocess.run(
            ['sudo', 'systemctl', 'stop', routing['service']],
            capture_output=True, text=True, timeout=15
        )
        return jsonify({
            'success': result.returncode == 0,
            'message': 'Agent stopped. SQS queue purged.',
            'output': result.stdout + result.stderr
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/delete-all-claims', methods=['POST'])
def api_delete_all_claims():
    """Delete all claims from DynamoDB for a fresh start."""
    try:
        table = dynamodb.Table('helixona-claims')
        scan = table.scan()
        delete_count = 0
        for item in scan.get('Items', []):
            cid = item['claim_id']
            table.delete_item(Key={'claim_id': cid})
            delete_count += 1
        return jsonify({'success': True, 'delete_count': delete_count, 'message': f'Deleted {delete_count} claims. Next pipeline run will rediscover from ECW.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ──────────────────────────────────────────────────────────
# SUBMISSION AUDIT LOG
# ──────────────────────────────────────────────────────────
# One row per submission attempt, append-only, written by
# src/audit/submission_log.py. This is the record shown to a payer who
# disputes having received documentation.

AUDIT_COLUMNS = [
    ('submitted_date_pt', 'Date'),
    ('submitted_time_pt', 'Time (PT)'),
    ('method', 'Method'),
    ('submission_form_type', 'Form type'),
    ('claim_id', 'Claim #'),
    ('blueshield_claim_number', 'BS claim #'),
    ('dos', 'DOS'),
    ('patient_name', 'Patient'),
    ('subscriber_id', 'Subscriber ID'),
    ('documents_label', 'Documents submitted'),
    ('outcome', 'Outcome'),
    ('fln', 'FLN'),
    ('linkage_label', 'Attached to prior claim?'),
    ('notes', 'Notes'),
]

DOC_LABELS = {
    'hcfa': 'HCFA-1500',
    'prog_notes': 'IV Note',
    'encounter': 'Progress Note',
}


def _linkage_risk(row):
    """True when a replacement claim was transmitted as a brand-new one.

    The claim itself is coded as a replacement, but the packet went to the
    payer under a first-submission form type — so the payer opens a fresh
    claim instead of attaching the documents to the one being disputed. This
    is the single most useful thing the log can surface: it marks exactly the
    submissions a payer is likely to report as never received.
    """
    return (
        str(row.get('claim_submission_type', '')).strip().lower() == 'resubmission'
        and 'first submission' in str(row.get('submission_form_type', '')).lower()
    )


def _load_audit_rows():
    """Fetch and normalise every audit row, newest first."""
    rows = scan_all(dynamodb.Table('helixona-submissions'))

    out = []
    for r in rows:
        # Rows predating the audit log (written by the Stage 6 stub) lack the
        # timestamp fields. Skip them rather than render blank lines.
        if not r.get('submitted_at_utc'):
            continue
        for k, v in list(r.items()):
            if hasattr(v, 'real') and not isinstance(v, bool):
                r[k] = int(v)
        docs = r.get('documents') or []
        r['documents_label'] = ', '.join(
            DOC_LABELS.get(d.get('document', ''), d.get('document', '?')) for d in docs
        )
        r['linkage_risk'] = _linkage_risk(r)
        r['linkage_label'] = 'No — sent as a new claim' if r['linkage_risk'] else 'Yes'
        out.append(r)

    out.sort(key=lambda r: r.get('submitted_at_utc', ''), reverse=True)
    _mark_superseded(out)
    return out


def _mark_superseded(rows):
    """Flag failed attempts that a later successful submission resolved.

    A claim that failed at 15:24 and went out cleanly at 15:49 does not have a
    problem, and showing the failure alongside the success reads as though it
    does. The row is kept — an append-only log that quietly drops evidence is
    not an audit log — but it is marked so the default view can leave it out.

    Only *earlier* failures count. A failure after the last success is a live
    problem and stays visible.
    """
    latest_success = {}
    for r in rows:
        if r.get('outcome') == 'submitted':
            cid = str(r.get('claim_id', ''))
            when = r.get('submitted_at_utc', '')
            if when > latest_success.get(cid, ''):
                latest_success[cid] = when

    for r in rows:
        r['superseded'] = bool(
            r.get('outcome') != 'submitted'
            and r.get('submitted_at_utc', '')
            < latest_success.get(str(r.get('claim_id', '')), '')
        )


def _filter_audit_rows(rows):
    """Apply the ?q= quick search plus ?from= / ?to= / ?outcome= / ?flag= filters.

    ?q= is one box over everything a payer would quote back at you — patient
    name, either claim number, date of service, subscriber ID. Asking staff to
    know which field a number belongs to is a step they should not have to take.
    The older per-field ?claim= / ?patient= params still work.
    """
    q = (request.args.get('q') or '').strip().lower()
    claim = (request.args.get('claim') or '').strip()
    patient = (request.args.get('patient') or '').strip().lower()
    date_from = (request.args.get('from') or '').strip()
    date_to = (request.args.get('to') or '').strip()
    outcome = (request.args.get('outcome') or '').strip().lower()
    flag = (request.args.get('flag') or '').strip().lower()
    sub_type = (request.args.get('type') or '').strip().lower()
    # A failed attempt that a later submission resolved is hidden by default.
    show_superseded = (request.args.get('superseded') or '').strip() in ('1', 'true', 'yes')

    def keep(r):
        if r.get('superseded') and not show_superseded:
            return False
        if sub_type and sub_type not in str(r.get('claim_submission_type', '')).lower():
            return False
        if q:
            haystack = ' '.join(str(r.get(k, '')) for k in (
                'patient_name', 'claim_id', 'blueshield_claim_number',
                'dos', 'subscriber_id', 'fln')).lower()
            if q not in haystack:
                return False
        if claim and claim not in (str(r.get('claim_id', '')),
                                   str(r.get('blueshield_claim_number', ''))):
            return False
        if patient and patient not in str(r.get('patient_name', '')).lower():
            return False
        if date_from and str(r.get('submitted_date_pt', '')) < date_from:
            return False
        if date_to and str(r.get('submitted_date_pt', '')) > date_to:
            return False
        if outcome and str(r.get('outcome', '')).lower() != outcome:
            return False
        if flag == 'unlinked' and not r.get('linkage_risk'):
            return False
        if flag == 'no-fln' and str(r.get('fln', '')):
            return False
        return True

    return [r for r in rows if keep(r)]


@app.route('/api/audit-log')
def api_audit_log():
    try:
        rows = _filter_audit_rows(_load_audit_rows())
        return jsonify({'rows': rows, 'count': len(rows)})
    except Exception as e:
        return jsonify({'rows': [], 'count': 0, 'error': str(e)})


@app.route('/api/audit-log.csv')
def api_audit_log_csv():
    """CSV export — the artefact you actually hand to a payer or auditor."""
    import csv
    import io

    try:
        rows = _filter_audit_rows(_load_audit_rows())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _, label in AUDIT_COLUMNS])
    for r in rows:
        writer.writerow([r.get(key, '') for key, _ in AUDIT_COLUMNS])

    stamp = datetime.utcnow().strftime('%Y%m%d')
    return app.response_class(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition':
                 f'attachment; filename=helixona-submission-audit-{stamp}.csv'},
    )

AUDIT_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Submission Audit Log — Helixona</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;500;600;700&family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#07070a;--bg2:#0c0c12;--panel:#0f0f16;--panel2:#15151e;--card:#14141c;--card2:#1a1a24;
  --bdr:#1f1f2b;--bdr2:#2a2a38;
  --accent:#CDB486;--accent2:#b09968;--accent-glow:rgba(205,180,134,.18);
  --success:#10b981;--warning:#f59e0b;--bad:#ef4444;--info:#3b82f6;
  --text-primary:#f1f5f9;--text-secondary:#94a3b8;--text-muted:#64748b;--text-dim:#475569;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{background:var(--bg);color:var(--text-primary);font-family:'Inter',sans-serif;font-size:13px;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
input,select,button{font-family:'Inter',sans-serif}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bdr2);border-radius:3px}

.wrap{max-width:1500px;margin:0 auto;padding:22px 26px 70px}

/* ---------- header ---------- */
.top{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;flex-wrap:wrap;margin-bottom:20px}
h1{font-family:'Playfair Display',serif;font-size:26px;font-weight:600;color:var(--accent);letter-spacing:-.3px}
.sub{color:var(--text-secondary);font-size:13px;margin-top:5px;max-width:640px;line-height:1.5}
.back{font-size:12px;color:var(--text-muted);border:1px solid var(--bdr);padding:8px 13px;border-radius:8px;transition:all .15s;white-space:nowrap}
.back:hover{border-color:var(--accent);color:var(--accent)}

/* ---------- stat tiles ---------- */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));gap:12px;margin-bottom:20px}
.tile{background:linear-gradient(180deg,var(--panel) 0%,var(--bg2) 100%);border:1px solid var(--bdr);border-radius:13px;padding:15px 17px;position:relative;overflow:hidden}
.tile .k{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--text-muted);font-weight:600;margin-bottom:7px;display:flex;align-items:center;gap:5px}
.tile .v{font-size:29px;font-weight:700;letter-spacing:-1px;line-height:1;color:var(--text-primary)}
.tile .v.sm{font-size:15px;font-weight:600;letter-spacing:-.2px;padding-top:7px}
.tile .n{font-size:11px;color:var(--text-muted);margin-top:6px;line-height:1.4}
.tile.hero .v{color:var(--accent);text-shadow:0 0 24px var(--accent-glow)}
.tile.warn{border-color:rgba(245,158,11,.32)}
.tile.warn .v{color:var(--warning)}
.tile.crit{border-color:rgba(239,68,68,.34)}
.tile.crit .v{color:var(--bad)}
.tile.act{cursor:pointer;transition:all .15s}
.tile.act:hover{border-color:var(--accent);transform:translateY(-1px)}
.tile.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent-glow)}
.dot{width:7px;height:7px;border-radius:50%;flex:none}
.dot.w{background:var(--warning)} .dot.c{background:var(--bad)} .dot.g{background:var(--success)}

/* ---------- controls ---------- */
.bar{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;background:var(--panel);border:1px solid var(--bdr);border-radius:13px;padding:14px 16px;margin-bottom:16px}
.fld{display:flex;flex-direction:column;gap:5px}
.fld label{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted);font-weight:600}
.fld input,.fld select{background:var(--bg2);border:1px solid var(--bdr);color:var(--text-primary);padding:9px 11px;border-radius:8px;font-size:12.5px;outline:none;transition:border-color .15s}
.fld input:focus,.fld select:focus{border-color:var(--accent)}
/* Native date controls default to the light theme: a near-black calendar icon
   on a near-black field, and a white popup. color-scheme:dark hands the whole
   native widget — icon and calendar — to the browser's dark rendering. */
.fld input[type=date]{color-scheme:dark;cursor:pointer}
.fld input[type=date]::-webkit-calendar-picker-indicator{cursor:pointer;opacity:.65;transition:opacity .15s}
.fld input[type=date]:hover::-webkit-calendar-picker-indicator{opacity:1}
.fld.grow{flex:1;min-width:250px}
.fld.grow input{width:100%}
.fld input::placeholder{color:var(--text-dim)}
.btn{padding:9px 15px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;border:1px solid var(--bdr);background:var(--bg2);color:var(--text-secondary);transition:all .15s;white-space:nowrap}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.gold{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#000;border-color:var(--accent)}
.btn.gold:hover{filter:brightness(1.08);color:#000}

/* ---------- table ---------- */
.count{color:var(--text-secondary);font-size:12px;margin-bottom:10px;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.count b{color:var(--text-primary)}
.chip{background:var(--accent-glow);color:var(--accent);padding:3px 9px;border-radius:20px;font-size:11px;font-weight:600;cursor:pointer}
.chip:hover{background:rgba(205,180,134,.28)}
.panel{background:var(--panel);border:1px solid var(--bdr);border-radius:13px;overflow:hidden}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:900px}
th{background:var(--bg2);font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted);font-weight:600;text-align:left;padding:11px 14px;border-bottom:1px solid var(--bdr);position:sticky;top:0;z-index:2}
td{padding:11px 14px;border-bottom:1px solid var(--bdr);vertical-align:top;font-size:12.5px}
tr.row{cursor:pointer;transition:background .12s}
tr.row:hover{background:var(--card)}
tr.row.open{background:var(--card2)}
.mono{font-family:'JetBrains Mono','Monaco',monospace;font-size:11.5px;font-variant-numeric:tabular-nums}
.pt{color:var(--text-primary);font-weight:500}
.dim{color:var(--text-muted)}
.docs{color:var(--text-secondary);line-height:1.5}
.nowrap{white-space:nowrap}
.pill{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:20px;font-size:10.5px;font-weight:600;white-space:nowrap}
.pill.submitted{background:rgba(16,185,129,.13);color:var(--success)}
.pill.failed{background:rgba(239,68,68,.13);color:var(--bad)}
.pill.blocked{background:rgba(245,158,11,.13);color:var(--warning)}
.ty{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10.5px;font-weight:600;white-space:nowrap}
.ty.first{background:rgba(74,222,128,.12);color:#4ade80}
.ty.resub{background:rgba(245,158,11,.13);color:var(--warning)}
.caret{color:var(--text-dim);font-size:10px;transition:transform .15s;display:inline-block;width:11px}
tr.row.open .caret{transform:rotate(90deg);color:var(--accent)}

/* ---------- detail ---------- */
.detail td{background:var(--bg2);padding:0;border-bottom:1px solid var(--bdr)}
.dwrap{padding:17px 20px 19px;display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:17px}
.dsec h4{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--accent);font-weight:600;margin-bottom:9px}
.dl{display:flex;gap:9px;font-size:12px;padding:3px 0;line-height:1.5}
.dl .dt{color:var(--text-muted);min-width:96px;flex:none}
.dl .dd{color:var(--text-primary);word-break:break-word}
.doc{background:var(--card);border:1px solid var(--bdr);border-radius:8px;padding:9px 11px;margin-bottom:7px}
.doc .dn{display:block;font-weight:600;font-size:12px;color:var(--text-primary);margin-bottom:3px}
.doc a.dn{transition:color .15s}
.doc a.dn:hover{color:var(--accent)}
.doc a.dn .ext{font-weight:500;font-size:10.5px;color:var(--text-muted);margin-left:5px}
.doc a.dn:hover .ext{color:var(--accent)}
.doc .dm{font-size:10.5px;color:var(--text-muted);font-family:'JetBrains Mono','Monaco',monospace;word-break:break-all;line-height:1.5}
.empty{padding:52px 20px;text-align:center;color:var(--text-muted)}
.empty .big{font-size:15px;color:var(--text-secondary);margin-bottom:6px}
.more{padding:15px;text-align:center;border-top:1px solid var(--bdr)}
.loading{padding:52px;text-align:center;color:var(--text-muted)}
</style>
</head>
<body>
<div class="wrap">

  <div class="top">
    <div>
      <h1>Submission Audit Log</h1>
      <div class="sub">Every documentation packet sent to a payer — when it went, how, for which
      claim and date of service, and exactly which documents were included.</div>
    </div>
    <a class="back" href="/">&larr; Dashboard</a>
  </div>

  <div class="tiles" id="tiles"></div>

  <div class="bar">
    <div class="fld grow">
      <label>Search</label>
      <input id="q" placeholder="Patient, claim #, Blue Shield claim #, date of service, subscriber ID…" autocomplete="off">
    </div>
    <div class="fld"><label>From</label><input id="from" type="date"></div>
    <div class="fld"><label>To</label><input id="to" type="date"></div>
    <div class="fld"><label>Type</label>
      <select id="type">
        <option value="">All types</option>
        <option value="first time">New submission</option>
        <option value="resubmission">Resubmission</option>
      </select>
    </div>
    <div class="fld"><label>Outcome</label>
      <select id="outcome">
        <option value="">All</option>
        <option value="submitted">Submitted</option>
        <option value="failed">Failed</option>
        <option value="blocked">Blocked</option>
      </select>
    </div>
    <button class="btn" onclick="reset()">Clear</button>
    <a class="btn gold" id="csv" href="/api/audit-log.csv">Export CSV</a>
  </div>

  <div class="count" id="count">Loading…</div>
  <div class="panel">
    <div class="scroll">
      <table>
        <thead><tr>
          <th style="width:26px"></th>
          <th class="nowrap">Sent</th>
          <th>Patient</th>
          <th class="nowrap">Claim #</th>
          <th class="nowrap">Date of service</th>
          <th class="nowrap">Type</th>
          <th>Documents</th>
          <th class="nowrap">Status</th>
        </tr></thead>
        <tbody id="body"><tr><td colspan="8" class="loading">Loading submissions…</td></tr></tbody>
      </table>
    </div>
    <div class="more" id="more" style="display:none">
      <button class="btn" onclick="showMore()">Show more</button>
    </div>
  </div>
</div>

<script>
const PAGE = 60;
let ALL = [], SHOWN = [], limit = PAGE, flag = '', showSuperseded = false;

const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const DOCN = {hcfa:'HCFA-1500', prog_notes:'IV Note', encounter:'Progress Note'};

function qs() {
  const p = new URLSearchParams();
  for (const id of ['q','from','to','type','outcome']) {
    const v = document.getElementById(id).value.trim();
    if (v) p.set(id, v);
  }
  if (flag) p.set('flag', flag);
  if (showSuperseded) p.set('superseded', '1');
  return p.toString();
}

function tiles() {
  // Tiles describe what is currently on screen, not a fixed global total —
  // filtering to one patient should answer "how many of THEIRS went out
  // unattached", which is the question being asked at that moment.
  const view = SHOWN;
  const n = view.length;
  const filtered = n !== ALL.length;
  const bad = view.filter(r => r.outcome !== 'submitted').length;
  const dates = view.map(r => r.submitted_date_pt).filter(Boolean).sort();
  const range = dates.length ? dates[0] + ' → ' + dates[dates.length - 1] : '—';

  document.getElementById('tiles').innerHTML = `
    <div class="tile hero">
      <div class="k">${filtered ? 'Submissions in this view' : 'Submissions on record'}</div>
      <div class="v">${n.toLocaleString()}</div>
      <div class="n">${filtered ? 'of ' + ALL.length.toLocaleString() + ' on record' : 'packets sent to a payer'}</div>
    </div>
    <div class="tile">
      <div class="k">Period covered</div>
      <div class="v sm">${esc(range)}</div>
      <div class="n">first to most recent</div>
    </div>
    <div class="tile ${bad?'warn':''}">
      <div class="k"><span class="dot ${bad?'w':'g'}"></span>Failed or blocked</div>
      <div class="v">${bad.toLocaleString()}</div>
      <div class="n">never reached the payer</div>
    </div>`;
}

function setFlag(f) { flag = (flag === f) ? '' : f; limit = PAGE; apply(); }
function toggleSuperseded() { showSuperseded = !showSuperseded; limit = PAGE; apply(); }
function reset() {
  for (const id of ['q','from','to','type','outcome']) document.getElementById(id).value = '';
  flag = ''; showSuperseded = false; limit = PAGE; apply();
}

function rowHtml(r, i) {
  const docs = (r.documents || []).length
    ? (r.documents || []).map(d => DOCN[d.document] || d.document).join(', ')
    : '<span class="dim">none</span>';
  const bs = r.blueshield_claim_number
    ? `<div class="dim mono" style="font-size:10.5px">BS ${esc(r.blueshield_claim_number)}</div>` : '';
  const t = String(r.claim_submission_type || '');
  const typeCell = /resub/i.test(t) ? '<span class="ty resub">Resubmission</span>'
                 : /first/i.test(t) ? '<span class="ty first">New submission</span>'
                 : '<span class="dim">—</span>';
  return `<tr class="row" data-i="${i}" onclick="toggle(${i})">
    <td><span class="caret">&#9654;</span></td>
    <td class="nowrap mono">${esc(r.submitted_date_pt)}<div class="dim" style="font-size:10.5px">${esc(r.submitted_time_pt)} PT</div></td>
    <td class="pt">${esc(r.patient_name) || '<span class="dim">—</span>'}</td>
    <td class="nowrap mono">${esc(r.claim_id)}${bs}</td>
    <td class="nowrap mono">${esc(r.dos) || '<span class="dim">—</span>'}</td>
    <td class="nowrap">${typeCell}</td>
    <td class="docs">${docs}</td>
    <td><span class="pill ${esc(r.outcome)}">${esc(r.outcome)}</span></td>
  </tr>
  <tr class="detail" id="d${i}" style="display:none"><td colspan="8"></td></tr>`;
}

function detailHtml(r) {
  // Each document opens the PDF the dashboard already serves for that claim.
  const DOC_URL = {
    hcfa:       id => `/api/hcfa_pdf/${encodeURIComponent(id)}`,
    prog_notes: id => `/api/prog_notes/${encodeURIComponent(id)}`,
    encounter:  id => `/api/encounter_file/${encodeURIComponent(id)}`,
  };
  const docs = (r.documents || []).length ? (r.documents || []).map(d => {
    const name = esc(DOCN[d.document] || d.document);
    const url = DOC_URL[d.document] ? DOC_URL[d.document](r.claim_id) : '';
    const title = url
      ? `<a class="dn open" href="${url}" target="_blank" rel="noopener">${name} <span class="ext">open PDF &#8599;</span></a>`
      : `<div class="dn">${name}</div>`;
    const meta = [
      d.filename ? esc(d.filename) + (d.bytes ? ' · ' + Number(d.bytes).toLocaleString() + ' bytes' : '') : '',
      d.sha256 ? 'sha256 ' + esc(d.sha256).slice(0, 32) + '…' : '',
      esc(d.s3_path || ''),
    ].filter(Boolean).join('<br>');
    return `<div class="doc">${title}<div class="dm">${meta}</div></div>`;
  }).join('') : '<div class="dim">No documents were attached to this attempt.</div>';

  return `<div class="dwrap">
    <div class="dsec">
      <h4>Submission</h4>
      <div class="dl"><span class="dt">Date &amp; time</span><span class="dd">${esc(r.submitted_date_pt)} ${esc(r.submitted_time_pt)} PT</span></div>
      <div class="dl"><span class="dt">UTC</span><span class="dd mono">${esc(r.submitted_at_utc)}</span></div>
      <div class="dl"><span class="dt">Method</span><span class="dd">${esc(r.method)}</span></div>
      <div class="dl"><span class="dt">Form type</span><span class="dd">${esc(r.submission_form_type) || '<span class="dim">not recorded</span>'}</span></div>
      <div class="dl"><span class="dt">Outcome</span><span class="dd">${esc(r.outcome)}</span></div>
      <div class="dl"><span class="dt">Payer ack (FLN)</span><span class="dd">${r.fln ? esc(r.fln) : '<span class="dim">not captured</span>'}</span></div>
    </div>
    <div class="dsec">
      <h4>Claim</h4>
      <div class="dl"><span class="dt">Patient</span><span class="dd">${esc(r.patient_name)}</span></div>
      <div class="dl"><span class="dt">Claim # (ECW)</span><span class="dd mono">${esc(r.claim_id)}</span></div>
      <div class="dl"><span class="dt">Prior BS claim #</span><span class="dd mono">${esc(r.blueshield_claim_number) || '<span class="dim">none on file</span>'}</span></div>
      <div class="dl"><span class="dt">Date of service</span><span class="dd mono">${esc(r.dos)}</span></div>
      <div class="dl"><span class="dt">Subscriber ID</span><span class="dd mono">${esc(r.subscriber_id)}</span></div>
      <div class="dl"><span class="dt">Our class.</span><span class="dd">${esc(r.claim_submission_type) || '<span class="dim">—</span>'}</span></div>
    </div>
    <div class="dsec">
      <h4>Documents (${(r.documents || []).length})</h4>
      ${docs}
    </div>
    ${r.error ? `
    <div class="dsec">
      <h4>Error</h4>
      <div class="dl"><span class="dd">${esc(r.error)}</span></div>
    </div>` : ''}
  </div>`;
}

function toggle(i) {
  const d = document.getElementById('d' + i);
  const row = document.querySelector(`tr.row[data-i="${i}"]`);
  const open = d.style.display !== 'none';
  if (open) { d.style.display = 'none'; row.classList.remove('open'); return; }
  d.querySelector('td').innerHTML = detailHtml(SHOWN[i]);
  d.style.display = '';
  row.classList.add('open');
}

function render() {
  const body = document.getElementById('body');
  if (!SHOWN.length) {
    body.innerHTML = `<tr><td colspan="8" class="empty">
      <div class="big">No submissions match this search.</div>
      <div>Try a surname, a claim number, or a date of service like 07/23/2026.</div></td></tr>`;
    document.getElementById('more').style.display = 'none';
    return;
  }
  const slice = SHOWN.slice(0, limit);
  body.innerHTML = slice.map((r, i) => rowHtml(r, i)).join('');
  document.getElementById('more').style.display = SHOWN.length > limit ? '' : 'none';

  const active = flag === 'unlinked' ? ' <span class="chip" onclick="setFlag(\\'unlinked\\')">not attached to prior claim &times;</span>'
              : flag === 'no-fln' ? ' <span class="chip" onclick="setFlag(\\'no-fln\\')">no payer acknowledgement &times;</span>' : '';
  // Never hide rows silently — an audit log has to say what it is leaving out.
  const hidden = showSuperseded ? 0 : ALL.filter(r => r.superseded).length;
  const supLabel = showSuperseded
    ? ` <span class="chip" onclick="toggleSuperseded()">including retried attempts &times;</span>`
    : (hidden ? ` <span class="chip" onclick="toggleSuperseded()">${hidden} retried attempt${hidden === 1 ? '' : 's'} hidden — show</span>` : '');
  const term = document.getElementById('q').value.trim().toLowerCase();
  const exactCount = term ? SHOWN.filter(r =>
      String(r.claim_id || '').toLowerCase() === term
      || String(r.blueshield_claim_number || '').toLowerCase() === term).length : 0;
  const exactNote = exactCount
    ? ` <span class="chip">${exactCount} exact claim-number match${exactCount === 1 ? '' : 'es'} first</span>` : '';
  document.getElementById('count').innerHTML =
    `Showing <b>${slice.length.toLocaleString()}</b> of <b>${SHOWN.length.toLocaleString()}</b> submissions${active}${supLabel}${exactNote}`;
}

function showMore() { limit += PAGE; render(); }

// Filtering happens in the browser over the rows already in memory.
//
// It used to re-query the server on every keystroke, which was both slow and
// wrong. Slow because each keystroke re-scanned the whole table and shipped up
// to 1.7MB back. Wrong because the responses could land out of order: clearing
// the box asked for all 1188 rows while a one-row response for the text just
// deleted was still in flight, and the small one arrived last and won — so an
// empty search box showed a single result.
//
// The server-side filters stay exactly as they were; the CSV export and the
// API still use them.
function matches(r) {
  // A failure a later submission already resolved is not a problem to show.
  if (r.superseded && !showSuperseded) return false;
  const q = document.getElementById('q').value.trim().toLowerCase();
  if (q) {
    const hay = [r.patient_name, r.claim_id, r.blueshield_claim_number,
                 r.dos, r.subscriber_id, r.fln].join(' ').toLowerCase();
    if (!hay.includes(q)) return false;
  }
  const from = document.getElementById('from').value.trim();
  const to = document.getElementById('to').value.trim();
  const d = r.submitted_date_pt || '';
  if (from && d < from) return false;
  if (to && d > to) return false;

  const ty = document.getElementById('type').value.trim().toLowerCase();
  if (ty && !String(r.claim_submission_type || '').toLowerCase().includes(ty)) return false;

  const oc = document.getElementById('outcome').value.trim().toLowerCase();
  if (oc && String(r.outcome || '').toLowerCase() !== oc) return false;

  if (flag === 'unlinked' && !r.linkage_risk) return false;
  if (flag === 'no-fln' && r.fln) return false;
  return true;
}

function apply() {
  const q = qs();
  document.getElementById('csv').href = '/api/audit-log.csv' + (q ? '?' + q : '');
  SHOWN = ALL.filter(matches);

  // Searching a claim number matches it as a substring everywhere — "13" hits
  // 485 rows through dates, subscriber IDs and longer claim numbers, and the
  // claim actually numbered 13 is lost among them. Exact matches come first.
  const term = document.getElementById('q').value.trim().toLowerCase();
  if (term) {
    const exact = r => (String(r.claim_id || '').toLowerCase() === term
                     || String(r.blueshield_claim_number || '').toLowerCase() === term) ? 0 : 1;
    SHOWN = SHOWN.slice().sort((a, b) => exact(a) - exact(b)
      || String(b.submitted_at_utc || '').localeCompare(String(a.submitted_at_utc || '')));
  }

  limit = PAGE;
  tiles();
  render();
}

for (const id of ['q','from','to','type','outcome']) {
  document.getElementById(id).addEventListener('input', apply);
}

// Clicking a date field anywhere — not just the small calendar glyph — opens
// the picker. Typing dd.mm.yyyy by hand is the slow path; the calendar is the
// one people actually want.
for (const id of ['from','to']) {
  const el = document.getElementById(id);
  el.addEventListener('click', () => {
    if (typeof el.showPicker === 'function') {
      try { el.showPicker(); } catch (e) { /* not user-activated; ignore */ }
    }
  });
}

// One fetch of the whole log, then everything is local. Reload the page to
// pick up submissions made since.
(async function init() {
  try {
    const res = await fetch('/api/audit-log?superseded=1');
    const data = await res.json();
    ALL = data.rows || [];
    if (data.error) {
      document.getElementById('body').innerHTML =
        `<tr><td colspan="8" class="empty"><div class="big">Could not load the audit log.</div>
         <div>${esc(data.error)}</div></td></tr>`;
      document.getElementById('count').textContent = '';
      return;
    }
  } catch (e) {
    document.getElementById('body').innerHTML =
      `<tr><td colspan="8" class="empty"><div class="big">Could not reach the server.</div>
       <div>${esc(e.message || e)}</div></td></tr>`;
    document.getElementById('count').textContent = '';
    return;
  }
  apply();
})();
</script>
</body>
</html>
"""
@app.route('/audit')
def audit_page():
    return AUDIT_HTML


if __name__ == '__main__':
    print("\n" + "="*50)
    print("  Helixona Dashboard — http://localhost:5050")
    print("  Audit log      — http://localhost:5050/audit")
    print("="*50 + "\n")
    app.run(host='0.0.0.0', port=5050, debug=True)
