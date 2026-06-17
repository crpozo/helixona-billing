"""
Human-like browser interaction helpers.
Adds random delays and gradual typing to avoid bot detection.
"""
import random
import time


def human_delay(min_s: float = 0.5, max_s: float = 2.5):
    """Random pause like a human reading/thinking."""
    time.sleep(random.uniform(min_s, max_s))


def human_type(page, selector: str, text: str, min_delay: float = 0.04, max_delay: float = 0.12):
    """Type text character by character with human-like speed variations."""
    element = page.query_selector(selector)
    if element:
        element.click()
        human_delay(0.3, 0.8)
        # Clear existing content
        element.fill('')
        human_delay(0.2, 0.5)
        # Type each character with random delay
        for char in text:
            page.keyboard.type(char, delay=random.uniform(min_delay * 1000, max_delay * 1000))
            # Occasional longer pause (like thinking)
            if random.random() < 0.05:
                human_delay(0.3, 0.7)
        return True
    return False


def human_click(page, selector: str, timeout: int = 5000):
    """Click with a small random delay before clicking."""
    human_delay(0.3, 1.0)
    page.click(selector, timeout=timeout)
    human_delay(0.5, 1.5)
