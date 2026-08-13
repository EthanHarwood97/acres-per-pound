import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from acres_per_pound.landparse import parse_acres, has_land_keyword

CASES = [
    ("A superb family home with gardens and paddocks extending in all to just over 4 acres.", (4.0, None, 4.0, "acre", "partial")),
    ("The plot extends to approximately 2.5 acres.", (2.5, 2.5, 2.5, "acre", "approx")),
    ("Set within 0.75 hectares of grounds.", (1.853, 1.853, 1.853, "hectare", "exact")),
    ("A detached bungalow with a large garden of 0.4 of an acre.", (0.4, 0.4, 0.4, "acre", "exact")),
    ("Garden of half an acre with a paddock beyond.", (0.5, 0.5, 0.5, "acre", "exact")),
    ("The land measures one and a half acres in all.", (1.5, 1.5, 1.5, "acre", "exact")),
    ("Grounds extending to between 2.5 and 3 acres.", (2.5, 3.0, 2.75, "acre", "range")),
    ("In excess of 2 acres of paddock land.", (2.0, None, 2.0, "acre", "partial")),
    ("Just under 1 acre of garden.", (0.0, 1.0, 0.9, "acre", "partial")),
    ("Gardens and grounds of about 0.75 acres.", (0.75, 0.75, 0.75, "acre", "approx")),
    ("Surrounded by its own land in all about 1.2 hectares.", (2.965, 2.965, 2.965, "hectare", "approx")),
    ("A rare opportunity with 12 acres stms.", (12.0, 12.0, 12.0, "acre", "approx")),
    ("No garden to speak of, parking only.", None),
    ("Three bedroom semi-detached house on Acacia Avenue.", None),
    ("The house is located at 1 Acre Lane.", None),
    ("Offered with approximately 5,000 sq ft of garden.", (0.115, 0.115, 0.115, "sqft", "converted")),
    ("Set in two and a half acres of gardens and woodland.", (2.5, 2.5, 2.5, "acre", "exact")),
    ("A smallholding with 5.25 acres of pasture.", (5.25, 5.25, 5.25, "acre", "exact")),
    ("Equestrian facilities with 3 acres of grazing.", (3.0, 3.0, 3.0, "acre", "exact")),
    ("Approx 0.2 ha plot.", (0.494, 0.494, 0.494, "hectare", "approx")),
    ("Land in all measuring about 2.5 acres.", (2.5, 2.5, 2.5, "acre", "approx")),
]

fail = 0
for text, expected in CASES:
    got = parse_acres(text)
    if expected is None:
        ok = got is None
        detail = f"got {got[0:6] if got else None}"
    else:
        ok = got is not None
        detail = ""
        if got is not None:
            e_min, e_max, e_mid, e_unit, e_conf = expected
            ok = (
                abs(got[0] - e_min) < 0.01
                and ((got[1] is None) == (e_max is None))
                and (e_max is None or abs(got[1] - e_max) < 0.01)
                and abs(got[2] - e_mid) < 0.01
                and got[3] == e_unit
                and got[4] == e_conf
            )
            detail = f"got {got[0]}/{got[1]}/{got[2]} {got[3]} {got[4]} '{got[5]}'"
    status = "PASS" if ok else "FAIL"
    if not ok:
        fail += 1
    print(f"{status} | {text[:70]:70} | {detail}")

print(f"\n{len(CASES) - fail}/{len(CASES)} passed")
sys.exit(1 if fail else 0)
