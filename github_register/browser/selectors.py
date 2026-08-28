"""Centralized GitHub DOM selectors.

When GitHub ships a DOM change, this file is the ONLY place to touch — the
flow modules read these lists and never inline selector strings. Each list
is ordered by specificity: first entry is the current DOM, later entries
are fallbacks for older layouts.

Rules of thumb baked in here:
- The signup page has 3 forms: 2 OAuth (/sessions/social/*) and 1 main
  (action contains 'signup'). Never click OAuth buttons by accident.
- The verify page uses 8 single-digit inputs (#launch-code-0..7), not one.
"""
from __future__ import annotations

# --- signup form fields -----------------------------------------------------
EMAIL_INPUTS = ["#email", "input[name='email']", "input[type='email']"]
PASSWORD_INPUTS = ["#password", "input[name='password']"]
USERNAME_INPUTS = ["#login", "input[name='login']"]

# --- verification (launch code) ---------------------------------------------
OTP_INPUTS = [
    "#otp",
    "input[name='otp']",
    "input[autocomplete='one-time-code']",
    "#launch-code-0",  # verify page: 8 single-digit boxes launch-code-0..7
]

# --- forms & submit buttons ---------------------------------------------------
SIGNUP_FORM = "form[action*='signup']"
SUBMIT_SELECTORS = [
    f"{SIGNUP_FORM} button[type='submit']",
    "#submit",
    "button[type='submit']",
]

# --- login page --------------------------------------------------------------
LOGIN_USER_INPUTS = ["#login_field", "input[name='login']", "input[type='text']"]
LOGIN_PASS_INPUTS = ["#password", "input[name='password']", "input[type='password']"]

# --- validation / errors -------------------------------------------------------
VALIDATION_ALERTS = "[role='alert'], .is-error, .error, .flash-error"
