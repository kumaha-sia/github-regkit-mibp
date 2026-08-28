"""DOM selectors for the CodeBuddy registration flow.

Two domains are covered here:
- CodeBuddy signup pages (www.codebuddy.ai)
- GitHub OAuth pages (github.com/login, 2FA, authorize consent)

Every selector is a list ordered by specificity: first entry is the
current DOM, later entries are fallbacks. When CodeBuddy ships a DOM
change, this file is the ONLY place to touch.
"""
from __future__ import annotations

# --- CodeBuddy signup page --------------------------------------------------

# Checkbox: "I confirm that I have read and acknowledge..."
AGREE_CHECKBOX = [
    "input[type='checkbox']",
    "label:has-text('confirm')",
    "input[id*='agree']",
    "input[id*='terms']",
]

# "Sign up with GitHub" button (Octocat icon)
GITHUB_SIGNUP_BUTTON = [
    "button:has-text('Sign up with GitHub')",
    "a:has-text('Sign up with GitHub')",
    "button:has-text('GitHub')",
    "[data-provider='github']",
    "a:has-text('GitHub')",
]

# "Log in" tab (when account already exists)
LOGIN_TAB = [
    "button:has-text('Log in')",
    "a:has-text('Log in')",
    "[role='tab']:has-text('Log in')",
]

# --- GitHub OAuth login page -----------------------------------------------

# Username/email field on the GitHub login page
LOGIN_USER_INPUTS = [
    "#login_field",
    "input[name='login']",
    "input[type='text']",
    "input[placeholder*='username']",
    "input[placeholder*='email']",
]

# Password field
LOGIN_PASS_INPUTS = [
    "#password",
    "input[name='password']",
    "input[type='password']",
]

# "Sign in" button (green, form action=/session)
SIGN_IN_BUTTON = [
    "form[action*='session'] input[type='submit']",
    "form[action*='session'] button[type='submit']",
    "button:has-text('Sign in')",
    "input[type='submit']",
]

# --- GitHub 2FA page -------------------------------------------------------

# 2FA code input (6 digits)
TWOFA_INPUTS = [
    "input[name='otp']",
    "input[autocomplete='one-time-code']",
    "input[placeholder='XXXXXX']",
    "input[type='text'][maxlength='6']",
    "#otp",
]

# "Verify" button (green)
TWOFA_VERIFY_BUTTON = [
    "button:has-text('Verify')",
    "input[type='submit'][value='Verify']",
    "button[type='submit']",
]

# "More options" fallback (SMS / recovery code)
TWOFA_MORE_OPTIONS = [
    "button:has-text('More options')",
    "a:has-text('More options')",
    "details:has-text('More options') summary",
]

# --- GitHub OAuth authorize consent page -----------------------------------

# "Authorize" button (green, grants access to the OAuth app)
AUTHORIZE_BUTTON = [
    "form[action*='authorize'] input[type='submit']",
    "form[action*='authorize'] button[type='submit']",
    "input[value='Authorize']",
    "button:has-text('Authorize')",
    "button[name='authorize']",
]

# "Cancel" button (fallback detection — we never click this)
CANCEL_BUTTON = [
    "button:has-text('Cancel')",
    "input[value='Cancel']",
]

# --- CodeBuddy region selection page ---------------------------------------

# Input field: "Registration location" (opens dropdown)
REGION_INPUT = [
    "input[placeholder*='location']",
    "input[placeholder*='region']",
    "input[placeholder*='Registration']",
    "input[type='text']",
]

# Search box inside the dropdown
REGION_SEARCH = [
    "input[placeholder*='Search countries']",
    "input[placeholder*='search']",
    "input[type='text']",
]

# Submit button (green, with CodeBuddy cat icon) — appears after a country is selected
REGION_SUBMIT = [
    "button:has-text('Submit')",
    "button[type='submit']",
    "button:has-text('Confirm')",
]

# --- Detection text markers (for page classification) -----------------------

# GitHub login page
LOGIN_MARKERS = (
    "sign in to github",
    "username or email address",
)

# GitHub 2FA page
TWOFA_MARKERS = (
    "two-factor authentication",
    "enter the code from your two-factor",
    "verify your account",
)

# GitHub OAuth authorize consent page
AUTHORIZE_MARKERS = (
    "wants access to your github account",
    "authorizing allows this app to",
)

# CodeBuddy region selection page
REGION_MARKERS = (
    "select registration region",
    "registration location",
)

# Already authorized / already connected
ALREADY_AUTHORIZED_MARKERS = (
    "already authorized",
    "already connected",
    "you have already authorized",
)

# Application suspended / error
APP_SUSPENDED_MARKERS = (
    "application suspended",
    "app suspended",
    "the oauth application",
)
