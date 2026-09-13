"""What `scripts/deploy.py` puts in a lambda's zip.

The import graph IS the bundle manifest: `deploy.py` walks a lambda's imports and resolves each
against src dir → lambdas root → module root → cross-module globs. Getting that resolution wrong
ships a zip that imports the wrong file, which no unit test catches — the lambda runs, calls the
wrong vendor API, and fails only against the real thing.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_a_lambdas_own_file_wins_over_the_same_name_elsewhere():
    """Several lambdas each carry their own `provider_stripe.py`, and each must bundle ITS own.

    The resolver searches src dir → lambdas root → module root → cross-module globs, and refuses
    when the GLOB tiers are ambiguous — two modules exporting the same shareable name would
    otherwise bundle whichever sorted first, silently. But a hit in the src dir is not ambiguous:
    it wins by position. Bundling the wrong provider adapter would ship a lambda that calls the
    wrong API and only fails against the real vendor.
    """
    src = REPO / "modules/payments/lambdas/payment_links"
    if not (src / "provider_stripe.py").exists():
        return  # the fold has not landed in this tree

    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'scripts'); import deploy;"
         "print(deploy._local_imports("
         f"'{src}/main.py', '{src.parent}', '{src.parent.parent}', {{}})['provider_stripe'])"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip() == str((src / "provider_stripe.py").resolve()), \
        f"bundled the wrong adapter: {out.stdout.strip()}"


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all deploy bundling tests passed")
