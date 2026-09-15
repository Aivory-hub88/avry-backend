"""
Agent roster — the canonical, public list of Aivory's six deployable agent
personas (id, first-name identity, title).

Exists so external integrations that need to offer "pick which Aivory agent
this talks to" (e.g. the Cerveau Odoo widget, docs/CERVEAU-ODOO-UI-WIDGET-PLAN.md)
have one place to fetch that roster from instead of hardcoding it, which
previously drifted independently across frontend/avry-user-dashboard (TS),
this backend's own AGENT_TYPES sets, and any third-party addon.

Public, unauthenticated: this is static, non-sensitive reference data (no
user/tenant data), the same trust level as a product's public marketing
copy. Keeping it auth-free means a consumer can populate a picker before it
has any Aivory credentials at all (e.g. an Odoo admin configuring the widget
for the first time).

NOTE: this file is the new source of truth. frontend/avry-user-dashboard's
AGENT_DISPLAY_NAMES (lib/workspaceAccess.ts) and this backend's other
AGENT_TYPES sets (app/routes/agent_profiles.py, app/services/telegram_service.py)
are NOT wired to read from here yet -- updating them to do so (or at least
to import AGENT_ROSTER's ids) is a follow-up, not done in this change.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["agent-roster"])

# Matches AGENT_DISPLAY_NAMES in frontend/avry-user-dashboard/lib/workspaceAccess.ts.
# Keep in sync manually until that follow-up lands.
AGENT_ROSTER = [
    {"agent_type": "autonomous", "name": "Geno", "title": "Generalist Agent"},
    {"agent_type": "customer_service", "name": "Teo", "title": "Ticket Ops Agent"},
    {"agent_type": "leads_qualifier", "name": "Lex", "title": "Leads Qualifier Agent"},
    {"agent_type": "finance_invoice_ops", "name": "Finn", "title": "Finance & Invoice Ops Agent"},
    {"agent_type": "office_assistant", "name": "Ofira", "title": "Office Assistant"},
    {"agent_type": "chief_of_staff", "name": "Aira", "title": "Chief of Staff Agent"},
]


@router.get("/agent-roster")
async def get_agent_roster():
    return {"agents": AGENT_ROSTER}
