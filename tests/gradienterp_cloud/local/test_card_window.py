"""The card window (`finishInPopup` in prod/gradienterp_cloud/web/app.js) shows the save-card
result. The error it shows carries text from the server's answer, so it is set as text: a crafted
card-return link can't put markup into a signed-in owner's tab."""

from pathlib import Path

APP = (Path(__file__).resolve().parents[3] / "prod" / "gradienterp_cloud" / "web" / "app.js").read_text()


def test_the_card_window_sets_its_message_as_text():
    fn = APP[APP.index("async function finishInPopup"):]
    fn = fn[:fn.index("\n}\n")]
    assert "innerHTML" not in fn
    assert "note.textContent = " in fn and "msg.error" in fn


if __name__ == "__main__":
    for _n in [k for k in dir() if k.startswith("test_")]:
        globals()[_n]()
        print(f"ok {_n}")
    print("all card window tests passed")
