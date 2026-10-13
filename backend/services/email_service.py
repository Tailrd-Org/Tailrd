"""Email delivery service using Resend API."""

import html
import logging
import os
import re
from typing import Optional
from urllib.parse import urlsplit

import resend

logger = logging.getLogger(__name__)

_EDGE_EMPHASIS_START = re.compile(r"^(?:\*{2,}|_{2,})\s*")
_EDGE_EMPHASIS_END = re.compile(r"\s*(?:\*{2,}|_{2,})\Z")
_ITALIC_WRAP = re.compile(r"([*_])(.+)\1")


def clean_company_name(name: Optional[str]) -> str:
    """A company name without markdown emphasis ("**Tesla**" -> "Tesla").

    The same cleanup the dashboard applies (frontend lib/companyLogo.ts
    cleanCompanyName), so an email shows the name, avatar letter and avatar
    colour the feed does. Scraped GitHub lists wrote the bold markers into
    the stored name.
    """
    s = (name or "").strip()
    s = _EDGE_EMPHASIS_START.sub("", s, count=1)
    s = _EDGE_EMPHASIS_END.sub("", s, count=1)
    # A balanced single-marker wrap ("*Tesla*") is italics too.
    italic = _ITALIC_WRAP.fullmatch(s)
    if italic:
        s = italic.group(2)
    return s.strip()


class EmailService:
    """Sends transactional emails via Resend."""

    def __init__(self):
        self.api_key: Optional[str] = os.getenv("RESEND_API_KEY")
        self.from_email: Optional[str] = os.getenv("RESEND_FROM_EMAIL")
        self.frontend_url: Optional[str] = os.getenv("FRONTEND_URL")

        if not self.api_key or not self.from_email:
            logger.warning(
                "RESEND_API_KEY or RESEND_FROM_EMAIL not set. "
                "Email sending will be skipped."
            )

    @property
    def is_configured(self) -> bool:
        """Check if email service has required configuration."""
        return bool(self.api_key and self.from_email)

    def _build_verification_link(self, token: str) -> str:
        """Construct the verification URL: {FRONTEND_URL}/verify-email?token={token}."""
        if not self.frontend_url:
            raise ValueError(
                "FRONTEND_URL environment variable is not set. "
                "Cannot build verification link."
            )
        # Strip any trailing slash so we don't emit `https://host//verify-email`,
        # which breaks the SPA route. Mirrors match_notifier's handling.
        base = self.frontend_url.rstrip("/")
        return f"{base}/verify-email?token={token}"

    def send_verification_email(self, to_email: str, token: str) -> bool:
        """
        Send a verification email with the token link.

        Args:
            to_email: Recipient email address.
            token: The verification token to embed in the link.

        Returns:
            True if email was sent successfully, False otherwise.
        """
        if not self.is_configured:
            logger.warning(
                "Email service not configured. Skipping verification email to %s.",
                to_email,
            )
            return False

        try:
            verification_link = self._build_verification_link(token)
        except ValueError as e:
            logger.error("Failed to build verification link: %s", e)
            return False

        html_content = self._build_email_html(verification_link)

        try:
            resend.api_key = self.api_key
            resend.Emails.send(
                {
                    "from": self.from_email,
                    "to": [to_email],
                    "subject": "Verify your email address",
                    "html": html_content,
                }
            )
            logger.info("Verification email sent to %s.", to_email)
            return True
        except Exception as e:
            logger.error("Failed to send verification email to %s: %s", to_email, e)
            return False

    def _build_email_html(self, verification_link: str) -> str:
        """Build the verification email, styled after Stripe's design system.

        Mirrors the tokens used in the job-match alert (getdesign stripe /
        DESIGN.md): indigo CTA #533afd, ink #0d253d, ink-mute #64748d,
        canvas-soft #f6f9fc, hairline #e3e8ee, pill buttons (9999px), and a
        gradient-mesh header band.
        """
        font = (
            "Inter, 'SF Pro Display', system-ui, -apple-system, "
            "BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
        )
        href = html.escape(verification_link, quote=True)
        shown = html.escape(verification_link)

        # Brand mark: the Tailrd logo sits on its own white header bar (where the
        # purple mark reads cleanly) instead of floating as a badge on the
        # gradient. Served from the frontend origin so it loads in the email
        # client; derive the origin from the verification link (falls back to
        # FRONTEND_URL) and only render the bar if we have a usable absolute URL.
        parts = urlsplit(verification_link)
        origin = (
            f"{parts.scheme}://{parts.netloc}"
            if parts.scheme and parts.netloc
            else (self.frontend_url or "").rstrip("/")
        )
        logo_header = ""
        if origin:
            logo_url = html.escape(f"{origin}/logo-full.png", quote=True)
            logo_header = f"""\
                    <!-- Logo bar (white) -->
                    <tr>
                        <td style="background-color: #ffffff; padding: 26px 32px 22px;">
                            <img src="{logo_url}" alt="Tailrd" height="30" style="display: block; height: 30px; width: auto; border: 0;" />
                        </td>
                    </tr>"""

        return f"""\
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; font-family: {font}; background-color: #f6f9fc; -webkit-font-smoothing: antialiased;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color: #f6f9fc; padding: 32px 16px;">
        <tr>
            <td align="center">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width: 480px; background-color: #ffffff; border-radius: 16px; overflow: hidden; border: 1px solid #e3e8ee; box-shadow: rgba(0,55,112,0.08) 0 8px 24px, rgba(0,55,112,0.04) 0 2px 6px;">
{logo_header}
                    <!-- Gradient mesh hero -->
                    <tr>
                        <td style="background: linear-gradient(120deg, #f5e9d4 0%, #f96bee 28%, #b9b9f9 52%, #533afd 78%, #ea2261 100%); padding: 28px 32px;">
                            <div style="font-size: 11px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; color: rgba(255,255,255,0.85);">
                                Email verification
                            </div>
                            <div style="margin-top: 10px; font-size: 26px; font-weight: 300; letter-spacing: -0.4px; line-height: 1.15; color: #ffffff;">
                                Verify your email
                            </div>
                        </td>
                    </tr>
                    <!-- Body -->
                    <tr>
                        <td style="padding: 28px 32px 8px;">
                            <p style="margin: 0 0 22px; font-size: 15px; font-weight: 300; line-height: 1.5; color: #64748d;">
                                Thanks for signing up. Confirm your email address to activate your account and start matching with roles built for you.
                            </p>
                            <!-- Pill CTA -->
                            <table role="presentation" cellpadding="0" cellspacing="0" style="margin: 0 0 24px;">
                                <tr>
                                    <td style="border-radius: 9999px; background-color: #533afd;">
                                        <a href="{href}" target="_blank" style="display: inline-block; padding: 12px 28px; font-size: 15px; font-weight: 500; color: #ffffff; text-decoration: none;">
                                            Verify email address
                                        </a>
                                    </td>
                                </tr>
                            </table>
                            <p style="margin: 0 0 8px; font-size: 13px; font-weight: 300; line-height: 1.5; color: #64748d;">
                                Or paste this link into your browser:
                            </p>
                            <p style="margin: 0 0 24px; font-size: 13px; font-weight: 300; line-height: 1.5; color: #533afd; word-break: break-all;">
                                {shown}
                            </p>
                        </td>
                    </tr>
                    <!-- Footer -->
                    <tr>
                        <td style="padding: 8px 32px 36px;">
                            <p style="margin: 20px 0 0; padding-top: 20px; border-top: 1px solid #e3e8ee; font-size: 12px; font-weight: 300; line-height: 1.5; color: #64748d;">
                                This link expires in 24 hours. If you didn't create a Tailrd account, you can safely ignore this email.
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""
    def send_job_match_alert(
        self,
        to_email: str,
        jobs: list[dict],
        recipient_name: Optional[str] = None,
        unsubscribe_url: Optional[str] = None,
    ) -> bool:
        """Send a digest of confident Strong matches (fit engine, SCORED, band Strong).

        Args:
            to_email: Recipient email address.
            jobs: List of match dicts, each with keys:
                title, company, match_score (the engine's displayed fit score, 0-100), apply_url,
                and optionally location, salary, posted.
                Should be pre-sorted by match_score descending.
            recipient_name: Optional first name for a personal greeting.
            unsubscribe_url: The recipient's opt-out link
                (services/alert_unsubscribe). Rendered in the footer and sent
                as List-Unsubscribe + List-Unsubscribe-Post (RFC 8058), which is
                what puts the mail client's own one-click "Unsubscribe" button
                next to the sender. The notifier passes one whenever
                FRONTEND_URL is set, which prod always has.

        Returns:
            True if the email was sent, False otherwise (not configured,
            empty job list, or send failure).
        """
        if not self.is_configured:
            logger.warning(
                "Email service not configured. Skipping match alert to %s.",
                to_email,
            )
            return False

        if not jobs:
            logger.info("No matches to send to %s; skipping alert.", to_email)
            return False

        top = jobs[0]
        # The engine naming (SPEC section 7): the band, then "fit score <n>", never a percent match.
        subject = f"Strong match: {top['title']} · fit score {top['match_score']}"
        html_content = self._build_job_alert_html(jobs, recipient_name, unsubscribe_url)

        params = {
            "from": self.from_email,
            "to": [to_email],
            "subject": subject,
            "html": html_content,
        }
        if unsubscribe_url:
            params["headers"] = {
                "List-Unsubscribe": f"<{unsubscribe_url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            }

        try:
            resend.api_key = self.api_key
            resend.Emails.send(params)
            logger.info("Match alert (%d jobs) sent to %s.", len(jobs), to_email)
            return True
        except Exception as e:
            logger.error("Failed to send match alert to %s: %s", to_email, e)
            return False

    def _build_job_alert_html(
        self,
        jobs: list[dict],
        recipient_name: Optional[str] = None,
        unsubscribe_url: Optional[str] = None,
    ) -> str:
        """Build the HTML for a job-match alert, styled after Stripe's system.

        Tokens (from getdesign stripe / DESIGN.md): indigo CTA #533afd, ink
        #0d253d, ink-mute #64748d, canvas-soft #f6f9fc, hairline #e3e8ee, pill
        buttons (9999px), and a gradient-mesh header band. Numeric/score cells
        use tabular figures (font-variant-numeric: tabular-nums).
        """
        font = (
            "Inter, 'SF Pro Display', system-ui, -apple-system, "
            "BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
        )
        greeting = (
            f"Hi {html.escape(recipient_name)}," if recipient_name else "Hi there,"
        )
        cards = "".join(self._build_job_card(job, font) for job in jobs)

        # There was never a toggle to "enable" alerts: every verified user with a
        # resume gets them, so the footer says so, and says how to stop them.
        if unsubscribe_url:
            href = html.escape(unsubscribe_url, quote=True)
            opt_out = (
                f'<a href="{href}" target="_blank" '
                'style="color: #533afd; text-decoration: underline;">'
                "Unsubscribe from match alerts</a>"
            )
        else:
            opt_out = "You can turn them off in Tailrd under Settings."

        # Brand logo chip in the header. Hosted at FRONTEND_URL/logo-full.png
        # (Vercel serves frontend/public at the site root). Sits on a white
        # rounded chip so the dark wordmark stays legible over the gradient.
        logo_block = (
            '<div style="font-size: 11px; font-weight: 600; letter-spacing: 1px; '
            'text-transform: uppercase; color: rgba(255,255,255,0.9);">'
            'Tailrd Instant Alert</div>'
        )
        if self.frontend_url:
            logo_src = html.escape(
                f"{self.frontend_url.rstrip('/')}/logo-full.png", quote=True
            )
            logo_block = (
                '<span style="display: inline-block; background-color: #ffffff; '
                'border-radius: 9999px; padding: 8px 16px; '
                'box-shadow: rgba(13,37,61,0.12) 0 1px 3px;">'
                f'<img src="{logo_src}" alt="Tailrd" height="20" '
                'style="display: block; height: 20px; border: 0;"></span>'
            )

        return f"""\
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; font-family: {font}; background-color: #f6f9fc; -webkit-font-smoothing: antialiased;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color: #f6f9fc; padding: 32px 16px;">
        <tr>
            <td align="center">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width: 520px; background-color: #ffffff; border-radius: 16px; overflow: hidden; border: 1px solid #e3e8ee; box-shadow: rgba(0,55,112,0.08) 0 8px 24px, rgba(0,55,112,0.04) 0 2px 6px;">
                    <!-- Gradient mesh header -->
                    <tr>
                        <td style="background: linear-gradient(120deg, #f5e9d4 0%, #f96bee 28%, #b9b9f9 52%, #533afd 78%, #ea2261 100%); padding: 32px 32px 28px;">
                            {logo_block}
                            <div style="margin-top: 18px; font-size: 26px; font-weight: 300; letter-spacing: -0.4px; line-height: 1.15; color: #ffffff;">
                                Always be the first to apply
                            </div>
                        </td>
                    </tr>
                    <!-- Intro -->
                    <tr>
                        <td style="padding: 28px 32px 8px;">
                            <p style="margin: 0 0 6px; font-size: 16px; font-weight: 300; color: #0d253d;">
                                {greeting}
                            </p>
                            <p style="margin: 0; font-size: 15px; font-weight: 300; line-height: 1.5; color: #64748d;">
                                We found {len(jobs)} new {"role" if len(jobs) == 1 else "roles"} that strongly match your resume. Apply early to stand out.
                            </p>
                        </td>
                    </tr>
                    <!-- Job cards -->
                    <tr>
                        <td style="padding: 16px 32px 8px;">
                            {cards}
                        </td>
                    </tr>
                    <!-- Footer -->
                    <tr>
                        <td style="padding: 16px 32px 36px;">
                            <p style="margin: 24px 0 4px; font-size: 14px; font-weight: 300; color: #0d253d;">
                                Happy job hunting,
                            </p>
                            <p style="margin: 0 0 20px; font-size: 14px; font-weight: 400; color: #0d253d;">
                                The Tailrd Team
                            </p>
                            <p style="margin: 0; font-size: 11px; font-weight: 300; line-height: 1.5; color: #64748d;">
                                You're getting match alerts because you uploaded a resume to Tailrd. {opt_out}
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""

    # Deterministic letter-avatar palette (mirrors frontend lib/companyLogo.ts
    # so email and dashboard agree on a company's fallback color).
    _AVATAR_COLORS = (
        "#7C6CFF", "#F97316", "#0EA5E9", "#22C55E", "#E11D48",
        "#A855F7", "#0891B2", "#2563EB", "#DB2777", "#059669",
        "#D97706", "#4F46E5",
    )

    def _avatar_color(self, company: str) -> str:
        s = clean_company_name(company) or "?"
        # Hashed over UTF-16 code units, as the dashboard's charCodeAt() does.
        units = s.encode("utf-16-le")
        h = 0
        for i in range(0, len(units), 2):
            h = (h * 31 + (units[i] | units[i + 1] << 8)) & 0xFFFFFFFF
        return self._AVATAR_COLORS[h % len(self._AVATAR_COLORS)]

    def _avatar_letter(self, company: str) -> str:
        """First letter or digit, uppercased; "?" when there is none."""
        for ch in clean_company_name(company):
            if ch.isalnum():
                return ch.upper()
        return "?"

    def _build_logo_cell(self, company_raw: str, logo_url: str) -> str:
        """A 40px rounded company logo, or a deterministic letter avatar.

        The avatar color sits behind the logo too, so a transparent/missing
        favicon degrades to a clean colored tile rather than a broken image.
        """
        color = self._avatar_color(company_raw)
        company = html.escape(clean_company_name(company_raw))
        if logo_url:
            safe = html.escape(logo_url, quote=True)
            inner = (
                f'<img src="{safe}" width="40" height="40" alt="{company}" '
                'style="display:block; width:40px; height:40px; border-radius:8px; '
                f'background-color:{color}; border:1px solid #e3e8ee;">'
            )
        else:
            letter = html.escape(self._avatar_letter(company_raw))
            inner = (
                '<div style="width:40px; height:40px; border-radius:8px; '
                f'background-color:{color}; color:#ffffff; text-align:center; '
                'line-height:40px; font-size:18px; font-weight:600;">'
                f'{letter}</div>'
            )
        return (
            '<td valign="top" width="54" style="width:54px; padding-right:14px;">'
            f'{inner}</td>'
        )

    def _build_job_card(self, job: dict, font: str) -> str:
        """Render a single job match as a Stripe-styled card."""
        company_raw = clean_company_name(str(job.get("company", "")))
        title = html.escape(str(job.get("title", "")))
        company = html.escape(company_raw)
        score = int(job.get("match_score", 0) or 0)
        apply_url = html.escape(str(job.get("apply_url", "#")), quote=True)
        logo_cell = self._build_logo_cell(company_raw, str(job.get("logo_url") or "").strip())

        location = html.escape(str(job.get("location") or "").strip())
        salary = html.escape(str(job.get("salary") or "").strip())
        posted = html.escape(str(job.get("posted") or "").strip())

        # Meta line: salary · location
        meta_parts = [p for p in (salary, location) if p]
        meta_line = ""
        if meta_parts:
            meta_line = (
                '<div style="margin-top: 6px; font-size: 14px; font-weight: 300; '
                'color: #64748d; font-variant-numeric: tabular-nums;">'
                f'{" · ".join(meta_parts)}</div>'
            )

        posted_line = ""
        if posted:
            posted_line = (
                '<div style="margin-top: 6px; font-size: 13px; font-weight: 300; '
                'color: #64748d;">'
                f'{posted} · Be an early applicant</div>'
            )

        return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin: 0 0 16px; border: 1px solid #e3e8ee; border-radius: 12px;">
    <tr>
        <td style="padding: 20px 20px 18px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                    {logo_cell}
                    <td valign="top">
                        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                            <tr>
                                <td style="font-size: 13px; font-weight: 400; color: #61718a; letter-spacing: 0.2px;">
                                    {company}
                                </td>
                                <td align="right" style="white-space: nowrap;">
                                    <span style="display: inline-block; padding: 4px 10px; border-radius: 9999px; background-color: #b9b9f9; color: #4434d4; font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums;">
                                        Fit score {score}
                                    </span>
                                </td>
                            </tr>
                        </table>
                        <div style="margin-top: 8px; font-size: 18px; font-weight: 300; letter-spacing: -0.2px; line-height: 1.3; color: #0d253d;">
                            {title}
                        </div>
                        {meta_line}
                        {posted_line}
                        <table role="presentation" cellpadding="0" cellspacing="0" style="margin-top: 16px;">
                            <tr>
                                <td style="border-radius: 9999px; background-color: #533afd;">
                                    <a href="{apply_url}" target="_blank" style="display: inline-block; padding: 9px 20px; font-size: 14px; font-weight: 400; color: #ffffff; text-decoration: none;">
                                        APPLY NOW
                                    </a>
                                </td>
                            </tr>
                        </table>
                    </td>
                </tr>
            </table>
        </td>
    </tr>
</table>"""


# Module-level singleton, initialized once at import time
email_service = EmailService()
