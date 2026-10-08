"""Offline wiring smoke test — no Discord connection required.

Run:  python -m tests.smoke_bot

Verifies:
- database initialization + migrations
- extension loading and the shared /mm group with all 9 subcommands
- persistent view + modal registration (restart-recovery machinery)
- panel data builders render Components V2 containers (no accent bar,
  within the 4000-character text budget)
- the background monitor starts
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

EXPECTED_SUBCOMMANDS = {
    "setup", "status", "ticket", "ticketinfo",
    "reportinfo", "staff", "forceclaim", "release", "close",
}


async def _exercise(bot) -> None:
    from database.models import Ticket, Trader
    from views import register_ticket_views
    from views.confirmation_views import ConfirmationView, DeclineModal
    from views.mm_panel import AssignedView, ClaimView
    from views.report_views import ReportCategoryView
    from views.staff_views import ReportModView
    from views.ticket_views import CancelModal, PartnerModal, TicketPanelView

    # -- command tree ----------------------------------------------------
    top = {c.name for c in bot.tree.get_commands()}
    assert top == {"mm"}, f"expected only the mm group, got {top}"
    mm_group = next(c for c in bot.tree.get_commands() if c.name == "mm")
    subs = {c.name for c in mm_group.commands}
    missing = EXPECTED_SUBCOMMANDS - subs
    assert not missing, f"missing subcommands: {missing}"
    print(f"[ok] /mm group with {len(subs)} subcommands: {sorted(subs)}")

    # Every command must carry its permission check (guards against a silent
    # decorator-order regression that would drop authorization).
    for command in mm_group.commands:
        assert len(command.checks) >= 1, f"/mm {command.name} has NO permission check"
    print("[ok] all 9 commands carry permission checks")

    # -- persistent views ------------------------------------------------
    register_ticket_views(bot, 4242)
    from views.mm_panel import SetupPanelView

    checks = [
        SetupPanelView(),
        TicketPanelView(1),
        TicketPanelView(1, stage="intro"),
        TicketPanelView(1, stage="roles"),
        TicketPanelView(1, stage="flat"),
        ConfirmationView(1),
        ClaimView(1),
        AssignedView(1),
        ReportCategoryView(1),
        ReportModView(1),
    ]
    for view in checks:
        assert view.is_persistent(), f"{type(view).__name__}({getattr(view, 'stage', '')}) not persistent"
        # round-trip to components (catches row/width overflow errors)
        comps = view.to_components()
        assert comps and comps[0].get("type") == 17, (
            f"{type(view).__name__} did not render a V2 container"
        )
        assert comps[0].get("accent_color") is None, (
            f"{type(view).__name__} has an accent bar (should be borderless)"
        )
    print(f"[ok] {len(checks)} view types persistent, serialisable, borderless")

    # -- modals -----------------------------------------------------------
    for modal in (PartnerModal(1), CancelModal(1), DeclineModal(1)):
        assert modal.is_persistent(), f"{type(modal).__name__} is not persistent"
    print("[ok] modals are persistent (registerable across restarts)")

    # -- embed builders ---------------------------------------------------
    ticket = Ticket(
        id=1, ticket_number=42, guild_id=config.GUILD_ID or 1, channel_id=1234,
        creator_id=111, partner_id=222, status="MM_CLAIMED",
        created_at=1760000000, updated_at=1760000100, closed_at=None,
        claimed_mm_id=333, claimed_at=1760000095, partner_added_at=1760000050,
        confirm_started_at=1760000060, confirm_expires_at=1760001860,
        mm_requested_at=1760000090, last_activity_at=1760000090,
        stage_started_at=1760000090, warned_stage=None, flagged=0,
        channel_missing=0, archived=0, cancel_reason=None,
        status_message_id=1, confirm_message_id=2, mm_message_id=3,
    )
    traders = [
        Trader(id=1, ticket_id=1, user_id=111, trade_role="SELLER", confirmed=True, confirmed_at=1760000070),
        Trader(id=2, ticket_id=1, user_id=222, trade_role="BUYER", confirmed=False, confirmed_at=None),
    ]
    from cogs.mm import build_ticket_overview
    from utils.formatting import build_transcript
    from views.base import (
        build_assigned_data, build_confirmation_data, build_intro_data,
        build_mm_request_data, build_profile_data, build_status_data,
        build_terminal_data, plain_panel_view,
    )

    def text_budget(view) -> int:
        """Total characters across all TextDisplay blocks (V2 limit: 4000)."""
        total = 0
        stack = list(getattr(view, "_children", []))
        while stack:
            item = stack.pop()
            content = getattr(item, "content", None)
            if isinstance(content, str):
                total += len(content)
            stack.extend(getattr(item, "_children", []) or [])
        return total

    datas = {
        "intro": build_intro_data(ticket),
        "status": build_status_data(ticket, traders),
        "confirm": build_confirmation_data(ticket, traders),
        "confirmed": build_confirmation_data(ticket, traders, state="confirmed"),
        "declined": build_confirmation_data(ticket, traders, state="declined"),
        "mm_request": build_mm_request_data(ticket),
        "assigned": build_assigned_data(
            ticket, 333, "https://cdn.example.com/avatar.png"
        ),
        "profile": build_profile_data(ticket, 333, "ExampleMM", None),
        "terminal": build_terminal_data(ticket),
        "overview": build_ticket_overview(ticket, traders),
    }
    for name, data in datas.items():
        view = plain_panel_view(data)
        comps = view.to_components()
        assert comps and comps[0].get("type") == 17, f"{name}: no V2 container"
        budget = text_budget(view)
        assert budget <= 4000, f"{name}: {budget} chars exceeds the 4000-char limit"
    print(f"[ok] {len(datas)} panel data builders render V2 containers within budget")

    transcript = build_transcript(ticket, traders, [], [], [])
    assert "MM-0042" in transcript and "TIMELINE" in transcript
    print("[ok] transcript builder")

    # -- report card --------------------------------------------------------
    from database.models import Report
    from views.base import plain_panel_view as _plain
    from views.report_views import build_report_data

    report = Report(
        id=7, ticket_id=1, reporter_id=111, reported_mm_id=333,
        category="Scam / Fraud",
        description="Took the items and left the server. " + "x" * 900,
        created_at=1760000200, status="OPEN", channel_id=1234, message_id=9,
        resolved_at=None, resolved_by=None, prev_ticket_status="MM_CLAIMED",
    )
    report_data = await build_report_data(bot, report, ticket)
    report_view = _plain(report_data)
    report_comps = report_view.to_components()
    assert report_comps and report_comps[0].get("type") == 17, "report card: no V2 container"
    budget = text_budget(report_view)
    assert budget <= 4000, f"report card: {budget} chars exceeds the 4000-char limit"
    print(f"[ok] report card renders as V2 container ({budget} chars)")

    # -- monitor ------------------------------------------------------------
    cog = bot.get_cog("MMCog")
    assert cog is not None, "MMCog not loaded"
    assert cog.monitor_loop.is_running(), "monitor loop not running"
    print("[ok] background monitor running")


async def run() -> None:
    tmp = tempfile.TemporaryDirectory()
    try:
        config.DATABASE_PATH = Path(tmp.name) / "smoke.db"
        config.TRANSCRIPT_SAVE_DIR = Path(tmp.name) / "transcripts"
        config.SYNC_COMMANDS = False
        config.TRANSCRIPT_ENABLED = True

        from main import MiddlemanBot

        bot = MiddlemanBot()
        try:
            await bot.setup_hook()
            await _exercise(bot)
        finally:
            for extension in ("cogs.mm", "cogs.tickets", "cogs.reports", "cogs.staff"):
                try:
                    await bot.unload_extension(extension)
                except Exception:
                    pass
            if bot.db is not None:
                await bot.db.close()
        print("SMOKE OK")
    finally:
        shutil.rmtree(tmp.name, ignore_errors=True)


def main() -> int:
    try:
        asyncio.run(run())
    except AssertionError as exc:
        print(f"SMOKE FAILED: {exc}")
        return 1
    except Exception as exc:  # pragma: no cover
        import traceback

        traceback.print_exc()
        print(f"SMOKE FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
