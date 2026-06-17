import pdfplumber

with pdfplumber.open("/tmp/hcfa_2676.pdf") as pdf:
    pg = pdf.pages[0]
    
    # Show ALL words near the Box 22 area (top ~440-490, x ~400-600)
    words = pg.extract_words()
    print("=== Words near ORIGINAL REF NO area (top 440-490) ===")
    for w in words:
        if 440 <= w["top"] <= 500:
            print(f"  x0={w['x0']:.0f} top={w['top']:.0f} text={repr(w['text'])}")
    
    print("\n=== Words with 'ORIGINAL' or 'RESUBMISSION' or 'REF' anywhere on page ===")
    for w in words:
        wt = w["text"].upper()
        if "ORIGINAL" in wt or "RESUBMISSION" in wt or wt == "REF." or wt == "REF" or wt == "NO.":
            print(f"  x0={w['x0']:.0f} top={w['top']:.0f} text={repr(w['text'])}")
    
    # Show line 20 context (lines 18-22)
    text = pg.extract_text() or ""
    lines = text.split("\n")
    print(f"\n=== Lines 18-22 of page 1 ({len(lines)} total lines) ===")
    for j in range(max(0,18), min(len(lines), 23)):
        print(f"  LINE {j}: {repr(lines[j])}")
