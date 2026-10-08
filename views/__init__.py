"""Views package.

Persistent views are registered:
- once at startup (setup panel),
- per active ticket at ticket creation and during restart recovery.

All custom IDs are deterministic (``mm:<action>:<ticket_id>``) so buttons
keep working across bot restarts.
"""

from __future__ import annotations

import logging

log = logging.getLogger("mm.views")

__all__ = ["register_panel_view", "register_ticket_views", "register_report_views"]


def register_panel_view(bot) -> None:
    """Register the global setup panel (mm:create)."""
    from views.mm_panel import SetupPanelView

    try:
        bot.add_view(SetupPanelView())
    except (ValueError, TypeError):
        log.exception("Could not register setup panel view")


def register_ticket_views(bot, ticket_id: int) -> None:
    """Register every persistent component belonging to one ticket."""
    from views.confirmation_views import ConfirmationView, DeclineModal
    from views.mm_panel import AssignedView, ClaimView, CompleteConfirmView, ReleaseConfirmView
    from views.report_views import ReportCategoryView
    from views.ticket_views import CancelApprovalView, CancelModal, PartnerModal, TicketPanelView

    views = [
        TicketPanelView(ticket_id),
        ConfirmationView(ticket_id),
        ClaimView(ticket_id),
        AssignedView(ticket_id),
        ReleaseConfirmView(ticket_id),
        CompleteConfirmView(ticket_id),
        CancelApprovalView(ticket_id),
        ReportCategoryView(ticket_id),
    ]
    for view in views:
        try:
            bot.add_view(view)
        except (ValueError, TypeError):
            log.exception("Could not register view %s for ticket %s", type(view).__name__, ticket_id)

    # Modals are registered so an open modal still submits after a restart.
    modals = [PartnerModal(ticket_id), CancelModal(ticket_id), DeclineModal(ticket_id)]
    for modal in modals:
        try:
            bot.add_view(modal)
        except (ValueError, TypeError):
            log.debug("Could not register modal %s for ticket %s", type(modal).__name__, ticket_id)


def register_report_views(bot, reports) -> None:
    """Register moderation buttons for open reports (restart recovery)."""
    from views.staff_views import ReportModView

    for report in reports:
        try:
            bot.add_view(ReportModView(report.id, resolved=report.status == "RESOLVED"))
        except (ValueError, TypeError):
            log.exception("Could not register view for report #%s", report.id)
