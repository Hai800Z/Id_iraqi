import time
from datetime import date

import pytest

tk = pytest.importorskip("tkinter")

from idcard_extractor import gui, pipeline  # noqa: E402
from idcard_extractor.config import load_settings  # noqa: E402
from idcard_extractor.models import CardSide, IdentityRecord, PipelineResult, ProcessedCard  # noqa: E402


class FakePipeline:
    """Stands in for the YOLO / PaddleOCR pipeline."""

    def __init__(self, settings):
        self.loaded = False

    def load_models(self):
        self.loaded = True

    def run(self, images, progress=None):
        front = CardSide("CARD_001", str(images[0]), 1, "iraq id card", None, 0.9, 0.9, True)
        record = IdentityRecord(first_name="أحمد", national_id="127901234567", date_of_birth=date(1979, 1, 5))
        for done, path in enumerate(images, start=1):
            if progress:
                progress(done, len(images), path)
        return PipelineResult(
            sides=[front],
            accepted=[ProcessedCard("CARD_001", True, "مقبولة", front=front, record=record)],
            rejected=[ProcessedCard("CARD_002", False, "لا يوجد وجه")],
        )


@pytest.fixture
def app(monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    shown = []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda title, message, name=name: shown.append((name, message)))
    monkeypatch.setattr(pipeline, "CardPipeline", FakePipeline)
    application = gui.App(root, load_settings())
    application.shown = shown
    yield application
    if not application.closed:
        application.close()


def wait_until_idle(app, timeout=10):
    deadline = time.monotonic() + timeout
    while app.running and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    app.root.update()


def test_run_writes_excel_and_shows_the_summary(app, tmp_path):
    photo = tmp_path / "front.jpg"
    photo.write_bytes(b"x")
    app.add_paths([photo, photo])  # duplicates are ignored
    assert app.inputs == [photo]

    app.excel_var.set(str(tmp_path / "out.xlsx"))
    app.start()
    wait_until_idle(app)

    assert not app.running
    assert (tmp_path / "out.xlsx").is_file()
    assert "المقبولة: 1" in app.summary_var.get() and "المرفوضة: 1" in app.summary_var.get()
    assert str(app.open_excel_button["state"]) == "normal"
    assert app.shown == []


def test_start_without_inputs_warns(app):
    app.start()
    assert app.shown[0][0] == "showwarning"
    assert not app.running


def test_missing_images_are_reported(app, tmp_path):
    app.add_paths([tmp_path / "missing-folder"])
    app.start()
    wait_until_idle(app)
    assert app.shown and app.shown[0][0] == "showerror"


def test_log_lines_reach_the_window(app):
    gui.log.warning("hello from the log")
    app._poll()
    assert "hello from the log" in app.log_text.get("1.0", "end")
