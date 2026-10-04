"""
mailer.py — transactional email sending for FPL IQ (account verification).

Uses Resend (https://resend.com) via its simple HTTP API when RESEND_API_KEY
is set. If no key is configured, it DEGRADES GRACEFULLY: it logs the email
(including the verification link) to stdout so the whole account flow can be
built and tested before the email service / domain are wired up.

Env vars:
  RESEND_API_KEY   your Resend API key (starts with 're_')
  MAIL_FROM        the 'from' address, e.g. 'FPL IQ <noreply@yourdomain>'
                   (defaults to Resend's shared test sender until you set a domain)
"""
from __future__ import annotations

import os

import requests

RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_FROM = "FPL IQ <onboarding@resend.dev>"  # swap to your domain later


def _from_address() -> str:
    return os.environ.get("MAIL_FROM", "").strip() or DEFAULT_FROM


def send_email(to: str, subject: str, html: str) -> dict:
    """Send an email via Resend. Returns {ok, error, skipped}.

    No key -> logs to stdout and returns {ok:True, skipped:True} so the
    account flow still works end-to-end in development."""
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        print("=" * 70)
        print("[mailer] RESEND_API_KEY not set — email NOT sent. Preview below:")
        print(f"  To:      {to}")
        print(f"  Subject: {subject}")
        print(f"  Body:\n{html}")
        print("=" * 70)
        return {"ok": True, "skipped": True, "error": None}
    try:
        resp = requests.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"from": _from_address(), "to": [to],
                  "subject": subject, "html": html},
            timeout=20,
        )
        if resp.status_code in (200, 201):
            return {"ok": True, "skipped": False, "error": None}
        return {"ok": False, "skipped": False,
                "error": f"Resend returned {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "skipped": False, "error": str(exc)}


def send_verification_email(to: str, verify_url: str) -> dict:
    """Send the account-verification email with the activation link."""
    subject = "Verify your FPL IQ account"
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:480px;margin:0 auto;
                background:#0b0f1a;color:#eef2fb;border-radius:16px;overflow:hidden">
      <div style="background:linear-gradient(135deg,#38035a,#ad1487);padding:24px;text-align:center">
        <div style="font-size:26px;font-weight:900;color:#fff;letter-spacing:1px">FPL IQ</div>
      </div>
      <div style="padding:28px 24px">
        <h2 style="font-size:19px;margin:0 0 10px">Confirm your email</h2>
        <p style="font-size:14px;color:#b9c2d6;line-height:1.6;margin:0 0 22px">
          Welcome to FPL IQ! Tap the button below to verify your email and activate your account.
          This link expires in 24 hours.
        </p>
        <a href="{verify_url}" style="display:inline-block;background:#37e6a4;color:#06130d;
           font-weight:800;font-size:15px;text-decoration:none;padding:14px 28px;border-radius:10px">
          Verify my account
        </a>
        <p style="font-size:12px;color:#8a97b4;line-height:1.5;margin:22px 0 0">
          Or paste this link into your browser:<br>
          <span style="color:#60a5fa;word-break:break-all">{verify_url}</span>
        </p>
        <p style="font-size:12px;color:#8a97b4;margin:22px 0 0">
          If you didn't create an FPL IQ account, you can safely ignore this email.
        </p>
      </div>
    </div>
    """
    return send_email(to, subject, html)
