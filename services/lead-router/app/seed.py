"""Demo data: six fictional Chicago-area attorneys and one exclusive territory.

All names and firms are made up, and emails use example.com, a domain reserved
for documentation, so nothing here can reach a real person.

Run:  python -m app.seed
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import Base, make_engine, make_session_factory, utcnow
from app.models import Attorney, Territory

DEMO_ATTORNEYS = [
    {
        "name": "Avery Stone",
        "firm": "Stone Injury Law (Demo)",
        "email": "avery.stone@example.com",
        "bar_states": ["IL"],
        "service_zips": ["606"],
        "bids_cents": {"personal_injury": 9500, "medical_malpractice": 12000},
        "plan": "pay_per_lead",
        "exo_username": "avery.stone",
    },
    {
        "name": "Jordan Reyes",
        "firm": "Reyes & Partners (Demo)",
        "email": "jordan.reyes@example.com",
        "bar_states": ["IL"],
        "service_zips": ["606", "607"],
        "bids_cents": {"personal_injury": 8000, "workers_comp": 6000},
        "plan": "pay_per_lead",
        "exo_username": "jordan.reyes",
    },
    {
        "name": "Sam Okafor",
        "firm": "Okafor Trial Group (Demo)",
        "email": "sam.okafor@example.com",
        "bar_states": ["IL", "IN"],
        "service_zips": ["606", "463"],
        "bids_cents": {"personal_injury": 7000, "employment": 5000},
        "plan": "pay_per_lead",
        "exo_username": "sam.okafor",
    },
    {
        "name": "Morgan Lee",
        "firm": "Lee Family Law (Demo)",
        "email": "morgan.lee@example.com",
        "bar_states": ["IL"],
        "service_zips": ["606"],
        "bids_cents": {"family_law": 4000, "estate_planning": 2500},
        "plan": "subscription",
        "monthly_fee_cents": 50000,
        "exo_username": "morgan.lee",
    },
    {
        "name": "Riley Chen",
        "firm": "Chen Defense Group (Demo)",
        "email": "riley.chen@example.com",
        "bar_states": ["IL"],
        "service_zips": ["606"],
        "bids_cents": {"criminal_defense": 4500, "family_law": 3500},
        "plan": "pay_per_lead",
        "exo_username": "riley.chen",
    },
    {
        "name": "Casey Patel",
        "firm": "Patel Bankruptcy & Debt (Demo)",
        "email": "casey.patel@example.com",
        "bar_states": ["IL"],
        "service_zips": ["605", "606"],
        "bids_cents": {"bankruptcy": 3000, "family_law": 2000},
        "plan": "pay_per_lead",
        "exo_username": "casey.patel",
    },
]

# Avery rents personal injury in Lincoln Park (60614) exclusively.
DEMO_TERRITORIES = [("Avery Stone", "personal_injury", "60614", 75000)]


def seed(db: Session) -> bool:
    """Load demo data once. Returns False if attorneys already exist."""
    if db.scalar(select(Attorney.id).limit(1)):
        return False
    by_name: dict[str, Attorney] = {}
    for data in DEMO_ATTORNEYS:
        attorney = Attorney(
            daily_cap=10,
            active=True,
            created_at=utcnow(),
            # Demo attorneys are fictional; a real onboarding records the bar numbers
            # checked on each state bar's website (POST .../license-check).
            license_numbers=dict.fromkeys(data["bar_states"], "DEMO"),
            license_verified_at=utcnow(),
            **data,
        )
        db.add(attorney)
        by_name[attorney.name] = attorney
    db.flush()
    for name, area, zip_code, fee in DEMO_TERRITORIES:
        db.add(
            Territory(
                attorney_id=by_name[name].id,
                practice_area=area,
                zip_code=zip_code,
                monthly_fee_cents=fee,
            )
        )
    db.commit()
    return True


def main() -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as db:
        print("Seeded demo attorneys." if seed(db) else "Attorneys already exist; nothing to do.")


if __name__ == "__main__":
    main()
