"""Seed the permit authority registry with German/Berlin authorities.

Run: ``python manage.py seed_permit_authorities``
The registry is multi-country by design (country/region columns); this seed
covers the current German reality (Berlin focus). Idempotent — skips codes
that already exist.
"""

from django.core.management.base import BaseCommand

from permits.models import PermitAuthority

SEED = [
    {
        "code": "DE-MUNI-BERLIN",
        "name": "Bezirksamt / Gemeinde Berlin",
        "authority_type": PermitAuthority.TYPE_MUNICIPALITY,
        "country": "DE",
        "region": "BE",
    },
    {
        "code": "DE-ROAD-BERLIN",
        "name": "Straßenbaulastträger Berlin (SenMVKU / Bezirke)",
        "authority_type": PermitAuthority.TYPE_ROAD,
        "country": "DE",
        "region": "BE",
    },
    {
        "code": "DE-ROAD-UNKNOWN",
        "name": "Road authority (to be resolved from road class)",
        "authority_type": PermitAuthority.TYPE_ROAD,
        "country": "DE",
        "region": "",
    },
    {
        "code": "DE-RAIL-DB",
        "name": "Deutsche Bahn / regional rail operator",
        "authority_type": PermitAuthority.TYPE_RAIL,
        "country": "DE",
        "region": "",
    },
    {
        "code": "DE-WATER-BERLIN",
        "name": "Berliner Wasserbehörde / water authority",
        "authority_type": PermitAuthority.TYPE_WATER,
        "country": "DE",
        "region": "BE",
    },
    {
        "code": "DE-ENV-BERLIN",
        "name": "Umweltbehörde Berlin (environmental agency)",
        "authority_type": PermitAuthority.TYPE_ENVIRONMENTAL,
        "country": "DE",
        "region": "BE",
    },
    {
        "code": "DE-TKG-ROW",
        "name": "Telecom right-of-way (TKG Wegerecht)",
        "authority_type": PermitAuthority.TYPE_TELECOM,
        "country": "DE",
        "region": "",
    },
]


class Command(BaseCommand):
    help = "Seed the German/Berlin permit authority registry (idempotent)."

    def handle(self, *args, **options):
        created = 0
        for item in SEED:
            _, was_created = PermitAuthority.objects.get_or_create(
                code=item["code"], defaults=item
            )
            created += int(was_created)
        self.stdout.write(
            self.style.SUCCESS(f"Permit authorities: {created} created, "
                               f"{len(SEED) - created} already present.")
        )
