"""The openlyoperated_biz stack (the public dashboard), locally.

    bash scripts/local-dev.sh --start
    open http://localhost:3001/                 # the dashboard, served from disk

The pages are files on CloudFront + S3, so the image serves `web/` off disk and nothing else: the
page reads the live api (api.openlyoperated.biz) and the live stream (events.openlyoperated.biz),
which have no local image — the read api is a REST api whose lambdas the snapshot lists but this
image does not route. What a page edit changes shows on reload against live data.

    python3 tests/server/snapshot.py openlyoperated_biz
"""

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from _image import REPO, build  # noqa: E402

# Its own table (counters) is not in `table-schemas.json` — that snapshot sweeps one CUSTOMER
# account and this one is operator-side. The static half is what the dashboard is developed against.
app = build(HERE / "image.json", tables=lambda t: False,
            static=REPO / "prod" / "openlyoperated_biz" / "web", static_html=True)   # CloudFront + S3: the pages are files
