"""Exercise desktop intake through real Qt drag/drop, buttons and key events.

Each scenario runs in a separate GUI process. This avoids QApplication versus
QCoreApplication lifetime conflicts and keeps native dialogs out of CI runs.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
try:
    from pipeline import find_tool
    DOVI = find_tool("dovi_tool")
except FileNotFoundError:
    DOVI = None

CHILD = r'''
import json
import sys
import threading
import time
import traceback
from fractions import Fraction
from pathlib import Path
import main
import pipeline
from PyQt5.QtCore import Qt, QMimeData, QPoint, QPointF, QTimer, QUrl
from PyQt5.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QMessageBox

fixture = Path(sys.argv[1])
case = sys.argv[2]
main.WORKSPACE = fixture / ('preferences-' + case)
PREFERENCE_CASES = {
    'prefs_v1_make': (1, 'bili_make', 'bili_dual'),
    'prefs_v1_bili_preserve': (1, 'bili_preserve', 'bili_dual'),
    'prefs_v1_preserve': (1, 'preserve', 'preserve'),
    'prefs_v1_quality': (1, 'quality', 'quality'),
    'prefs_v1_enhance': (1, 'enhance', 'enhance'),
    'prefs_v2_make': (2, 'bili_make', 'bili_make'),
    'prefs_v2_bili_preserve': (2, 'bili_preserve', 'bili_preserve'),
}
if case in PREFERENCE_CASES:
    version, saved_preset, _ = PREFERENCE_CASES[case]
    main.WORKSPACE.mkdir(parents=True, exist_ok=True)
    (main.WORKSPACE / 'ui_preferences.json').write_text(json.dumps({
        'version': version, 'export_preset': saved_preset,
        'audio_mode': 'aac', 'color_mode': 'hlg', 'crf': 19,
    }), encoding='utf-8')
pipeline.WORKSPACE = fixture / ('render-' + case)
pipeline.CHECKPOINT_FILE = pipeline.WORKSPACE / 'checkpoint.json'
main.CHECKPOINT_FILE = pipeline.CHECKPOINT_FILE
main.load_checkpoint = lambda: None
messages = []
for name in ('warning', 'critical', 'information'):
    setattr(QMessageBox, name, lambda *args: messages.append(str(args[-1])) or QMessageBox.Ok)
QMessageBox.question = lambda *args: QMessageBox.Yes
QMessageBox.exec_ = lambda self: messages.append(self.text() + '\n' + self.detailedText()) or QMessageBox.Ok
original_window = main.MainWindow
failed = []


def spin_until(predicate, label, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        QTest.qWait(5)
    raise AssertionError('Timed out waiting for ' + label)


class IntakeWindow(original_window):
    def __init__(self):
        super().__init__()
        self.import_events = []
        self.intake_finished.connect(lambda ok, detail: self.import_events.append((bool(ok), str(detail))))
        QTimer.singleShot(50, self.execute_scenario)

    def drop(self, paths, target=None, wait=True):
        widget = target if target is not None else self
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        before = len(self.import_events)
        enter = QDragEnterEvent(QPoint(12, 12), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(widget, enter)
        if enter.isAccepted():
            move = QDragMoveEvent(QPoint(12, 12), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
            QApplication.sendEvent(widget, move)
            event = QDropEvent(QPointF(12, 12), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
            QApplication.sendEvent(widget, event)
        if wait:
            spin_until(lambda: len(self.import_events) > before and not self.intake_busy,
                       'drop validation: ' + ', '.join(p.name for p in paths))
            return self.import_events[-1]
        QApplication.processEvents()
        return enter.isAccepted()

    def state(self):
        return (self.video.filepath, self.audio.filepath, self.output.text())

    def load_whole(self):
        ok, detail = self.drop([fixture / 'whole original.mp4'])
        assert ok, detail
        assert self.video.filepath == str(fixture / 'whole original.mp4')
        assert self.vinfo is not None and self.start.isEnabled()

    def execute_scenario(self):
        try:
            scenarios[case](self)
            print('INTAKE_UI_OK:' + case, flush=True)
        except BaseException:
            failed.append(traceback.format_exc())
            print(failed[-1], file=sys.stderr, flush=True)
        finally:
            if self.worker is not None:
                self.worker.request_stop()
                try:
                    spin_until(lambda: self.worker is None, 'worker shutdown', timeout=5)
                except BaseException:
                    failed.append(traceback.format_exc())
            self.close()
            try:
                spin_until(lambda: not self.intakes and not self.probes, 'inspection shutdown', timeout=5)
            except BaseException:
                failed.append(traceback.format_exc())
            QApplication.processEvents()
            QApplication.instance().quit()


def whole_original(w):
    assert not w.start.isEnabled(), 'start enabled without a valid source'
    assert not w.advanced_toggle.isChecked() and not w.advanced.isVisible(), 'advanced parameters are forced on a new user'
    w.load_whole()
    cfg = w.config()
    assert cfg['video'] == str(fixture / 'whole original.mp4')
    assert not cfg['audio'], 'dropping an ordinary whole clip unexpectedly selected a replacement soundtrack'
    assert cfg['audio_mode'] == 'atmos_auto' and cfg['require_atmos'], 'default target must generate or preserve real Atmos'
    assert cfg['route'] == 'transcode' and not cfg['ai'] and not cfg['interp'], 'ordinary intake should make Dolby without AI settings'
    assert cfg['delivery'] == 'bilibili' and cfg['color_mode'] == 'dv84'
    assert cfg['rpu_mode'] == 'auto' and not cfg['rpu'], 'automatic Dolby still requires a manual metadata file'
    assert Path(cfg['output']).suffix.lower() == '.mp4'
    assert Path(cfg['output']).resolve() != (fixture / 'whole original.mp4').resolve()
    assert not w.advanced_toggle.isChecked() and not w.advanced.isVisible(), 'a file drop opened technical settings automatically'
    summary = w.automatic_summary.text()
    assert summary and '原音' in summary, 'smart summary does not explain the automatic original-audio choice'
    assert '将制作杜比视界' in summary and 'Atmos' in summary
    assert Path(cfg['output']).name in summary, 'smart summary does not identify the automatic output'


def automatic_dolby_settings(w):
    w.load_whole()
    assert w.config()['hdr_remaster'] == 'auto' and w.config()['hdr_peak_nits'] == 600
    assert not w.advanced_toggle.isChecked(), 'automatic metadata opened advanced settings'
    # Controlled parsed dimensions exercise the preset without allocating or
    # encoding an 8K/120fps video. Real metadata/export has separate scenarios.
    w.vinfo.update(width=7680, height=4320, display_width=7680, display_height=4320,
                   fps=120, fps_fraction='120', color_transfer='arib-std-b67')
    w._preset_explicit = False
    w.choose_source_preset()
    cfg = w.config()
    assert (cfg['width'], cfg['height']) == (3840, 2160) and cfg['fps_override'] == 60
    assert not cfg['ai'] and not cfg['interp']
    assert '不重复扩展' in w.automatic_summary.text(), 'HDR input is described as receiving a second SDR expansion'
    QTest.mouseClick(w.advanced_toggle, Qt.LeftButton)
    choose_combo_with_keys(w.rpu_mode, 'external')
    assert w.rpu.isVisible() and w.rpu.isEnabled()
    w.rpu.set_file(str(fixture / 'manual metadata.bin'))
    assert w.config()['rpu_mode'] == 'external' and w.config()['rpu']
    choose_combo_with_keys(w.rpu_mode, 'auto')
    assert not w.config()['rpu'], 'automatic mode still injects the previously selected external RPU'
    choose_combo_with_keys(w.color, 'dv81')
    assert w.config()['rpu_mode'] == 'external', '8.1 incorrectly offers automatic metadata without a supported analysis path'
    assert not w.rpu_mode.model().item(0).isEnabled(), 'unsupported 8.1 automatic mode is selectable'
    choose_combo_with_keys(w.color, 'dv84')
    choose_combo_with_keys(w.rpu_mode, 'auto')
    assert not w.config()['rpu'] and w.config()['rpu_mode'] == 'auto'
    choose_combo_with_keys(w.export_preset, 'preserve')
    assert w.config()['route'] == 'preserve', 'manual original-video preservation is unavailable'


def missing_dolby_tool_is_actionable(w):
    w.load_whole()
    w.select_data(w.export_preset, 'bili_make')
    original = main.find_tool
    def missing(name):
        if name == 'dovi_tool':
            raise FileNotFoundError(name)
        return original(name)
    main.find_tool = missing
    try:
        target = Path(w.config()['output'])
        QTest.mouseClick(w.start, Qt.LeftButton)
        QApplication.processEvents()
        assert w.worker is None and w.outcome is None, 'missing Dolby tool started a degraded export'
        assert not target.exists()
        assert messages and 'setup.bat' in messages[-1] and 'dovi_tool' in messages[-1], 'missing tool has no installation remedy'
    finally:
        main.find_tool = original


def explicit_source_dynamic_range(w):
    w.load_whole()
    assert w.config()['source_transfer_override'] == 'auto'
    # Simulate an unresolved parsed result after stream and first-frame probing.
    # No HDR file is invented or encoded by this presentation-contract fixture.
    unresolved = dict(w.vinfo, color_transfer='unknown', color_primaries='bt2020', color_space='bt2020nc')
    w.on_probe(w.video.filepath, unresolved, '')
    assert not w.start.isEnabled(), 'unknown BT.2020 source was silently treated as SDR'
    assert '源动态范围' in w.automatic_summary.text() and '扩展 SDR' not in w.automatic_summary.text()
    QTest.mouseClick(w.advanced_toggle, Qt.LeftButton)
    choose_combo_with_keys(w.source_transfer, 'arib-std-b67')
    assert w.config()['source_transfer_override'] == 'arib-std-b67' and w.start.isEnabled()
    assert '不重复扩展' in w.automatic_summary.text() and w.top_status.text() == '请先配置 Atmos 编码器'
    choose_combo_with_keys(w.export_preset, 'preserve')
    assert w.config()['source_transfer_override'] == 'auto', 'original-video export sent a forbidden source interpretation override'
    assert w.source_transfer.currentData() == 'arib-std-b67' and not w.source_transfer.isEnabled(), 'preservation discarded the saved manual interpretation'
    choose_combo_with_keys(w.export_preset, 'bili_make')
    assert w.config()['source_transfer_override'] == 'arib-std-b67' and w.source_transfer.isEnabled(), 'returning to production lost the manual source interpretation'
    saved = dict(w.config())
    w.restore_config(saved)
    w.on_probe(w.video.filepath, unresolved, '')
    assert w.config()['source_transfer_override'] == 'arib-std-b67' and w.start.isEnabled()
    ok, detail = w.drop([fixture / 'whole original.mp4', fixture / 'replacement.wav'])
    assert ok, detail
    assert w.config()['source_transfer_override'] == 'arib-std-b67', 'same-source audio pairing discarded manual source interpretation'
    ok, detail = w.drop([fixture / 'second video.mp4'])
    assert ok, detail
    assert w.config()['source_transfer_override'] == 'auto', 'new source inherited the previous source interpretation'
    w.on_probe(w.video.filepath, dict(w.vinfo, color_transfer='unspecified', color_primaries='bt2020'), '')
    assert not w.start.isEnabled()
    w.select_data(w.export_preset, 'preserve')
    assert w.start.isEnabled() and w.config()['source_transfer_override'] == 'auto', 'original-video preservation unnecessarily requires color interpretation'


def paired_drop(w):
    # Audio comes first in the MIME list: dispatch must inspect all files.
    ok, detail = w.drop([fixture / 'replacement.wav', fixture / 'video only.mp4'])
    assert ok, detail
    cfg = w.config()
    assert cfg['video'] == str(fixture / 'video only.mp4')
    assert cfg['audio'] == str(fixture / 'replacement.wav')
    assert cfg['audio_mode'] == 'atmos_auto' and cfg['require_atmos'], 'paired WAV must enter Atmos generation rather than an ordinary codec fallback'


def audio_first(w):
    ok, detail = w.drop([fixture / 'replacement.wav'])
    assert ok, detail
    assert not w.video.filepath
    assert w.audio.filepath == str(fixture / 'replacement.wav')
    assert not w.start.isEnabled(), 'audio-only state must not start a video job'
    w.load_whole()
    assert w.config()['audio'] == str(fixture / 'replacement.wav'), 'adding video silently discarded audio selected first'


def audio_only_container(w):
    ok, detail = w.drop([fixture / 'audio inside.mp4'])
    assert ok, detail
    assert not w.video.filepath, 'an .mp4 extension was mistaken for a video track'
    assert w.audio.filepath == str(fixture / 'audio inside.mp4')
    assert not w.start.isEnabled()
    w.load_whole()
    assert w.config()['audio'] == str(fixture / 'audio inside.mp4')


def replace_and_restore(w):
    w.load_whole()
    ok, detail = w.drop([fixture / 'replacement.wav'], target=w.audio)
    assert ok, detail
    assert w.audio.filepath == str(fixture / 'replacement.wav')
    assert w.audio_clear.isEnabled()
    QTest.mouseClick(w.audio_clear, Qt.LeftButton)
    QApplication.processEvents()
    assert not w.audio.filepath
    assert not w.config()['audio']
    assert w.config()['audio_mode'] == 'atmos_auto'
    assert w.video.filepath == str(fixture / 'whole original.mp4')


def audio_from_video_section(w):
    ok, detail = w.drop([fixture / 'video only.mp4'], target=w.video)
    assert ok, detail
    ok, detail = w.drop([fixture / 'whole original.mp4'], target=w.audio)
    assert ok, detail
    assert w.config()['video'] == str(fixture / 'video only.mp4')
    assert w.config()['audio'] == str(fixture / 'whole original.mp4'), 'audio section must accept a video container as an audio source'


def reject_multiple_video(w):
    w.load_whole()
    before = w.state()
    ok, detail = w.drop([fixture / 'video only.mp4', fixture / 'second video.mp4'])
    assert not ok, 'multiple videos were accepted while only one can be represented'
    assert w.state() == before, 'multiple-video drop silently replaced or discarded a source'
    assert detail and w.notification.text(), 'rejection must explain what the user needs to change'
    ok, detail = w.drop([fixture / 'replacement.wav', fixture / 'audio inside.mp4'])
    assert not ok and detail, 'multiple audio sources were silently truncated'
    assert w.state() == before


def reject_invalid_atomically(w):
    w.load_whole()
    ok, detail = w.drop([fixture / 'replacement.wav'], target=w.audio)
    assert ok, detail
    before = w.state()
    ok, detail = w.drop([fixture / 'broken.mp4'])
    assert not ok and detail
    assert w.state() == before, 'invalid input replaced a valid source or its output'
    ok, detail = w.drop([fixture / 'second video.mp4', fixture / 'broken.wav'])
    assert not ok and detail
    assert w.state() == before, 'multi-file intake committed its valid half before validation finished'
    assert w.start.isEnabled(), 'a rejected file left the previously valid source unusable'


def automatic_and_user_output(w):
    w.load_whole()
    initial = w.output.text()
    ok, detail = w.drop([fixture / 'second video.mp4'])
    assert ok, detail
    assert w.output.text() != initial, 'automatic output stayed attached to the previous source'
    custom = str(fixture / 'chosen final output.mp4')
    w.output.setFocus()
    QApplication.clipboard().setText(custom)
    QTest.keyClick(w.output, Qt.Key_A, Qt.ControlModifier)
    QTest.keyClick(w.output, Qt.Key_V, Qt.ControlModifier)
    QApplication.processEvents()
    assert w.output.text() == custom, 'keyboard editing did not reach output field'
    ok, detail = w.drop([fixture / 'whole original.mp4'])
    assert ok, detail
    assert w.output.text() == custom, 'a new source overwrote the user-selected output path'


def choose_combo_with_keys(combo, value):
    def matches(actual):
        if isinstance(value, (tuple, list)):
            return isinstance(actual, (tuple, list)) and tuple(actual) == tuple(value)
        return actual == value
    index = next((i for i in range(combo.count()) if matches(combo.itemData(i))), -1)
    assert index >= 0, 'requested setting is unavailable: ' + str(value)
    assert combo.isEnabled(), 'manual setting is unexpectedly disabled'
    combo.setFocus()
    QTest.keyClick(combo, Qt.Key_Home)
    for _ in range(index):
        QTest.keyClick(combo, Qt.Key_Down)
    QApplication.processEvents()
    assert matches(combo.currentData()), 'manual setting did not receive user key events'


def picture_settings(w):
    cfg = w.config()
    keys = ('route', 'width', 'height', 'target_fps', 'color_mode', 'rpu_mode', 'hdr_remaster', 'hdr_peak_nits', 'source_transfer_override', 'ai', 'interp',
            'model', 'preset', 'crf', 'batch_size')
    return {key: cfg[key] for key in keys} | {
        'fps_override': cfg.get('fps_override'),
        'preset_choice': w.export_preset.currentData(),
        'ai_checked': w.ai.isChecked(), 'interp_checked': w.interp.isChecked(),
    }


def toggle_checkbox_with_space(checkbox):
    assert checkbox.isVisible() and checkbox.isEnabled(), 'checkbox is not available to the user'
    before = checkbox.isChecked()
    checkbox.setFocus()
    # A stretched QCheckBox may have its center outside the indicator/text hit
    # area on another desktop font. Space activates the focused real control.
    QTest.keyClick(checkbox, Qt.Key_Space)
    QApplication.processEvents()
    assert checkbox.isChecked() != before, 'focused checkbox ignored the user Space key'


def same_source_keeps_manual_picture_settings(w):
    w.load_whole()
    QTest.mouseClick(w.advanced_toggle, Qt.LeftButton)
    QApplication.processEvents()
    # Only tool availability is controlled. The drops below still inspect real
    # files asynchronously, and no model or AI processing is invoked.
    w.sr_available = w.interp_available = True
    for preset in ('quality', 'enhance'):
        choose_combo_with_keys(w.export_preset, preset)
        if preset == 'enhance':
            assert w.ai.isChecked() and w.interp.isChecked(), 'enhance preset did not enable the available tools'
            toggle_checkbox_with_space(w.ai)
            toggle_checkbox_with_space(w.interp)
            assert not w.ai.isChecked() and not w.interp.isChecked(), 'manual enhancement selections did not turn off'
        choose_combo_with_keys(w.resolution, (1920, 1080))
        choose_combo_with_keys(w.fps, 24)
        choose_combo_with_keys(w.color, 'hlg')
        expected = picture_settings(w)
        assert expected['width'] == 1920 and expected['target_fps'] == 24
        assert expected['color_mode'] == 'hlg' and not expected['ai'] and not expected['interp']
        ok, detail = w.drop([fixture / 'whole original.mp4'])
        assert ok, detail
        assert picture_settings(w) == expected, 'same-video reimport reset manual picture settings for ' + preset
        ok, detail = w.drop([fixture / 'replacement.wav', fixture / 'whole original.mp4'])
        assert ok, detail
        assert w.audio.filepath == str(fixture / 'replacement.wav')
        assert w.audio_mode.currentData() == 'aac', 'new WAV soundtrack was not made MP4-compatible'
        assert picture_settings(w) == expected, 'same-video plus replacement audio reset manual picture settings for ' + preset


def restored_picture_survives_inspection(w):
    w.load_whole()
    w.sr_available = w.interp_available = True
    QTest.mouseClick(w.advanced_toggle, Qt.LeftButton)
    choose_combo_with_keys(w.export_preset, 'enhance')
    assert w.ai.isChecked() and w.interp.isChecked(), 'enhance preset did not enable the available tools'
    toggle_checkbox_with_space(w.interp)
    for combo, value in ((w.resolution, (1920, 1080)), (w.fps, 48),
                         (w.color, 'hlg'), (w.audio_mode, 'aac'),
                         (w.hdr_remaster, 'off'),
                         (w.source_transfer, 'bt709'),
                         (w.model, 'realesr-animevideov3'), (w.encoding_preset, 'fast')):
        choose_combo_with_keys(combo, value)
    for spinbox, value in ((w.crf, '21'), (w.batch_size, '8')):
        spinbox.setFocus()
        QTest.keyClick(spinbox, Qt.Key_A, Qt.ControlModifier)
        QTest.keyClicks(spinbox, value)
    QApplication.processEvents()
    # Save a configuration actually produced by the UI, including a manual
    # frame-rate choice with interpolation disabled, then restore it afresh.
    saved = dict(w.config())
    expected = picture_settings(w)
    w.video.set_file('')
    w.restore_config(saved)
    assert picture_settings(w) == expected, 'restoring a task discarded manually chosen picture settings'
    assert expected['route'] == 'enhance' and expected['width'] == 1920
    assert expected['target_fps'] == 48, 'restoring a task discarded its requested frame rate'
    assert expected['color_mode'] == 'hlg' and expected['ai'] and not expected['interp']
    QTest.qWait(450)
    spin_until(lambda: not w.probes and not w.probe_timer.isActive(), 'restored picture source inspection')
    assert picture_settings(w) == expected, 'late inspection reset restored picture settings'
    assert w.audio_mode.currentData() == 'aac'
    ok, detail = w.drop([fixture / 'whole original.mp4'])
    assert ok, detail
    assert picture_settings(w) == expected, 'same-video reimport reset a restored task'


def asynchronous_validation(w):
    import quality
    original = quality.inspect_media
    released = threading.Event()
    entered = threading.Event()
    heartbeat = []
    def delayed(*args, **kwargs):
        entered.set()
        if not released.wait(1.5):
            raise AssertionError('the GUI event loop could not release the inspector')
        return original(*args, **kwargs)
    quality.inspect_media = delayed
    started = time.monotonic()
    QTimer.singleShot(50, lambda: (heartbeat.append(time.monotonic()), released.set()))
    try:
        ok, detail = w.drop([fixture / 'whole original.mp4'])
        assert ok, detail
        assert entered.is_set(), 'test did not exercise the real asynchronous inspector'
        assert heartbeat and heartbeat[0] - started < 1, 'media inspection blocked the GUI thread'
    finally:
        released.set()
        quality.inspect_media = original


def processing_locks_intake(w):
    w.load_whole()
    # This scenario exercises processing locks independently of the optional
    # Dolby executable. The default-Dolby route is verified in export scenarios.
    w.select_data(w.export_preset, 'quality')
    entered = threading.Event()
    class HeldPipeline:
        def __init__(self, cfg, log, progress, stage):
            self.stop = threading.Event()
        def cancel(self):
            self.stop.set()
        def set_paused(self, value):
            pass
        def run(self):
            entered.set()
            if not self.stop.wait(6):
                raise RuntimeError('test worker was not stopped')
            raise main.PipelineCancelled('test stop')
    original = main.Pipeline
    main.Pipeline = HeldPipeline
    try:
        QTest.mouseClick(w.start, Qt.LeftButton)
        spin_until(lambda: entered.is_set() and w.worker is not None, 'actual worker start')
        before = w.state()
        assert not w.start.isEnabled()
        w.drop([fixture / 'second video.mp4'], wait=False)
        QTest.qWait(50)
        assert w.state() == before, 'processing source changed during a live worker'
        assert not w.import_files([str(fixture / 'replacement.wav')]), 'public intake bypassed the processing lock'
        assert w.state() == before
        QTest.mouseClick(w.stop, Qt.LeftButton)
        spin_until(lambda: w.worker is None, 'stop and unlock')
        ok, detail = w.drop([fixture / 'second video.mp4'])
        assert ok, detail
        assert w.video.filepath == str(fixture / 'second video.mp4')
    finally:
        main.Pipeline = original


def paired_drop_finishes_export(w):
    ok, detail = w.drop([fixture / 'video only.mp4', fixture / 'replacement.wav'])
    assert ok, detail
    # This real encode covers the explicitly selected DV-only route. A
    # licensed Atmos encoder is a separate integration prerequisite.
    w.select_data(w.export_preset, 'bili_make')
    target = fixture / 'actual GUI export.mp4'
    w.output.setText(str(target))
    QTest.mouseClick(w.start, Qt.LeftButton)
    spin_until(lambda: w.worker is not None or w.outcome is not None, 'export start')
    spin_until(lambda: w.worker is None and w.outcome is not None, 'actual GUI export', timeout=15)
    assert w.outcome[0] == 'ok', 'paired media could not be exported with the automatically selected settings: ' + str(w.outcome)
    assert target.is_file() and target.stat().st_size > 0
    from quality import inspect_media
    result = inspect_media(target)
    assert result['audio_codec'] in ('aac', 'eac3'), 'replacement audio did not reach the final video'
    assert result['width'] == 192 and result['height'] == 128
    assert result['dv_profile'] == 8 and result['dv_compatibility'] == 4
    w._preset_explicit = False
    ok, detail = w.drop([target])
    assert ok, detail
    assert w.export_preset.currentData() == 'bili_dual' and w.config()['route'] == 'preserve', 'a completed compliant Dolby picture was unnecessarily remade for the dual target'
    assert w.config()['require_atmos'], 'picture preservation must not drop the double-Dolby audio requirement'


def failed_export_keeps_ui_usable(w):
    w.load_whole()
    w.select_data(w.export_preset, 'quality')
    original = main.Pipeline
    class FailedPipeline:
        def __init__(self, cfg, **_):
            pass
        def set_paused(self, value):
            pass
        def run(self):
            raise RuntimeError('controlled metadata failure for error-dialog coverage')
    main.Pipeline = FailedPipeline
    try:
        QTest.mouseClick(w.start, Qt.LeftButton)
        spin_until(lambda: w.worker is None and w.outcome is not None, 'error result and input unlock')
        assert w.outcome[0] == 'error' and 'controlled metadata failure' in w.outcome[1]
        assert messages and 'controlled metadata failure' in messages[-1], 'instance error dialog did not expose the actual failure'
        assert w.start.isEnabled() and w.input_group.isEnabled() and w.import_button.isEnabled()
        assert not Path(w.output.text()).exists(), 'failed task published an output'
        ok, detail = w.drop([fixture / 'second video.mp4'])
        assert ok, detail
    finally:
        main.Pipeline = original


def compatible_native_export(w, source_name, source_codec):
    ok, detail = w.drop([fixture / source_name])
    assert ok, detail
    w.select_data(w.export_preset, 'bili_make')
    assert not w.advanced_toggle.isChecked() and not w.advanced.isVisible(), 'a compatibility decision forced manual settings'
    assert w.vinfo['codec_name'] == source_codec, 'test did not exercise the intended source codec'
    cfg = w.config()
    assert cfg['route'] == 'transcode', source_name + ' was routed to an MP4 bitstream copy that cannot succeed'
    assert cfg['color_mode'] == 'dv84' and cfg['rpu_mode'] == 'auto' and not cfg['rpu']
    assert not cfg['ai'] and not cfg['interp'], 'format compatibility unexpectedly enabled AI processing'
    assert (cfg['width'], cfg['height']) == (192, 128)
    target = Path(cfg['output'])
    assert target.suffix.lower() == '.mp4'
    # Use the automatically proposed output and press the actual primary action;
    # dimensions, cadence and picture conversion remain automatic.
    QTest.mouseClick(w.start, Qt.LeftButton)
    spin_until(lambda: w.worker is not None or w.outcome is not None, 'smart compatibility export start')
    spin_until(lambda: w.worker is None and w.outcome is not None, 'smart compatibility export', timeout=15)
    assert w.outcome[0] == 'ok', 'automatic compatibility conversion failed for ' + source_name + ': ' + str(w.outcome)
    assert target.is_file() and target.stat().st_size > 0
    from quality import inspect_media
    result = inspect_media(target, count_frames=True)
    assert result['codec_name'] == 'hevc' and result['bit_depth'] == 10
    assert (result['width'], result['height']) == (192, 128)
    assert Fraction(result['fps_fraction']) == 6 and result['total_frames'] == 6
    assert result['color_transfer'] == 'arib-std-b67' and result['color_primaries'] == 'bt2020'
    assert result['color_space'] == 'bt2020nc'
    assert result['dv_profile'] == 8 and result['dv_compatibility'] == 4 and result['dv_rpu_present'] is True


def unsupported_copy_gets_native_export(w):
    compatible_native_export(w, 'lossless FFV1.mkv', 'ffv1')


def transport_stream_gets_native_export(w):
    compatible_native_export(w, 'broadcast H264.ts', 'h264')


def maximized_window_controls(w):
    assert w.isMaximized() and not w.isFullScreen(), 'startup must be a normal maximized application, keeping the desktop work area'
    flags = w.windowFlags()
    assert flags & Qt.WindowTitleHint, 'native title bar was removed'
    assert flags & Qt.WindowSystemMenuHint, 'native system menu was removed'
    assert not flags & Qt.FramelessWindowHint, 'application must not replace the normal desktop frame'
    assert (Path(main.__file__).resolve().parent / 'assets' / 'app.svg').is_file(), 'application icon asset is missing'
    assert not QApplication.windowIcon().isNull(), 'application has no icon'
    assert not w.windowIcon().isNull(), 'desktop window has no icon'
    assert len(QApplication.windowIcon().availableSizes()) >= 3, 'application icon is not available at desktop sizes'
    assert w.window_toggle.isVisible(), 'maximized window has no visible restore control'
    def font_pixels(widget):
        font = widget.font()
        return font.pixelSize() if font.pixelSize() > 0 else font.pointSizeF() * widget.logicalDpiY() / 72
    assert font_pixels(w) >= 15.5, 'normal application text is below 16px / 12pt'
    assert font_pixels(w.output) >= 15.5, 'output text remains too small'
    assert font_pixels(w.start) >= 15.5 and w.start.height() >= 40, 'primary action is too small to read or operate'
    w.activateWindow()
    w.setFocus()
    QApplication.processEvents()
    QTest.mouseClick(w.window_toggle, Qt.LeftButton)
    spin_until(lambda: not w.isMaximized() and not w.isFullScreen(), 'restore normal window')
    w.resize(1000, 640)
    QApplication.processEvents()
    assert w.height() <= 680, 'larger type forces the window beyond a small desktop'
    assert w.start.isVisible(), 'primary action disappeared on a small window'
    assert w.start.mapTo(w, QPoint(0, 0)).y() + w.start.height() <= w.height(), 'primary action is clipped below the window'
    QTest.mouseClick(w.window_toggle, Qt.LeftButton)
    spin_until(lambda: w.isMaximized() and not w.isFullScreen(), 'maximize normal application')
    QTest.keyClick(w, Qt.Key_F11)
    QApplication.processEvents()
    assert w.isMaximized() and not w.isFullScreen(), 'F11 unexpectedly entered exclusive fullscreen'
    QTest.keyClick(w, Qt.Key_Escape)
    QApplication.processEvents()
    assert w.isVisible() and w.isMaximized() and not w.isFullScreen(), 'Escape changed normal application windowing'


def audio_mode_key(w, value):
    if not w.advanced.isVisible():
        QTest.mouseClick(w.advanced_toggle, Qt.LeftButton)
        QApplication.processEvents()
    index = w.audio_mode.findData(value)
    assert index >= 0, 'requested audio mode is unavailable'
    w.audio_mode.setFocus()
    QTest.keyClick(w.audio_mode, Qt.Key_Home)
    for _ in range(index):
        QTest.keyClick(w.audio_mode, Qt.Key_Down)
    QApplication.processEvents()
    assert w.audio_mode.currentData() == value, 'audio selection did not receive the user key events'


def parsed_audio_fixture(codec):
    # Media decoding/detection has its own real-file tests. Here a controlled
    # parsed-result fixture drives presentation of an already-confirmed source;
    # this does not create or claim to create an Atmos-encoded audio file.
    import quality
    original = quality.inspect_audio
    def inspected(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if Path(path).resolve() == (fixture / 'replacement.wav').resolve():
            profile = 'Dolby Digital Plus + Dolby Atmos' if codec == 'eac3' else 'Dolby TrueHD + Dolby Atmos'
            info.update(audio_codec=codec, audio_profile=profile, atmos='confirmed', atmos_evidence='UI parsed-result fixture')
            info['audio_streams'] = [dict(info['audio_streams'][0], codec_name=codec, profile=profile)]
        return info
    quality.inspect_audio = inspected
    return quality, original


def atmos_mode_notice(w):
    w.load_whole()
    w.select_data(w.export_preset, 'bili_make')
    quality, original = parsed_audio_fixture('eac3')
    try:
        ok, detail = w.drop([fixture / 'replacement.wav'], target=w.audio)
        assert ok, detail
        assert w.audio_mode.currentData() == 'copy'
        audio_mode_key(w, 'aac')
        assert '将保留原码流' not in w.audio_hint.text(), 'AAC selection still promises Atmos bitstream preservation'
        assert '不保留' in w.audio_hint.text() or '无法保留' in w.audio_hint.text(), 'Atmos loss must be explained before encoding'
        # A late source probe after resume must not reset an intentional codec.
        saved = dict(w.config())
        saved['audio_mode'] = 'aac'
        w.video.set_file('')
        w.restore_config(saved)
        QTest.qWait(450)
        spin_until(lambda: not w.probes and not w.probe_timer.isActive(), 'restored source inspection')
        assert w.audio_mode.currentData() == 'aac', 'late source inspection overwrote restored audio settings'
        audio_mode_key(w, 'none')
        assert '将保留原码流' not in w.audio_hint.text(), 'muted export still promises an Atmos audio track'
        assert not w.config()['audio'], 'muted export still selected a replacement audio file'
    finally:
        quality.inspect_audio = original


def truehd_conversion_notice(w):
    w.load_whole()
    w.select_data(w.export_preset, 'bili_make')
    quality, original = parsed_audio_fixture('truehd')
    try:
        ok, detail = w.drop([fixture / 'replacement.wav'], target=w.audio)
        assert ok, detail
        assert w.audio_mode.currentData() == 'aac', 'an unsupported TrueHD copy was selected automatically'
        assert '将保留原码流' not in w.audio_hint.text(), 'TrueHD conversion incorrectly promises Atmos preservation'
        assert '不保留' in w.audio_hint.text() or '无法保留' in w.audio_hint.text()
    finally:
        quality.inspect_audio = original


def dual_target_blocks_missing_encoder(w):
    w.load_whole()
    assert w.export_preset.currentData() == 'bili_dual'
    cfg = w.config()
    assert cfg['require_atmos'] and cfg['audio_mode'] == 'atmos_auto'
    original_find = main.find_tool
    main.find_tool = lambda name: 'checked-tool' if name == 'dovi_tool' else original_find(name)
    w.atmos_backend_status = lambda: {'available': False, 'message': '请配置已授权的 Atmos 编码器'}
    try:
        w.refresh_start_state()
        assert w.top_status.text() == '请先配置 Atmos 编码器'
        assert '请先配置 Atmos 编码器' in w.stage_label.text() and '请先配置 Atmos 编码器' in w.drop_hint.text()
        assert '请先配置 Atmos 编码器' in w.notification.text()
        assert w.start.isEnabled() and w.start.text() == '配置 Atmos…'
        prior_messages = len(messages)
        QTest.mouseClick(w.start, Qt.LeftButton)
        QApplication.processEvents()
        assert w.worker is None and w.outcome is None and not Path(cfg['output']).exists()
        assert len(messages) == prior_messages, 'configuration action opened a redundant warning dialog'
        assert w.advanced_toggle.isChecked() and w.atmos_group.isVisible()
        assert w.config()['require_atmos'], 'missing encoder silently changed the product target'
        ok, detail = w.drop([fixture / 'video only.mp4'])
        assert ok, detail
        assert '音轨' in w.atmos_preflight_error(w.config())
        assert '拖入音频' in w.automatic_summary.text()
        assert w.top_status.text() == '请添加音轨'
        assert '请添加音轨' in w.stage_label.text() and '请添加音轨' in w.drop_hint.text()
        assert w.start.text() == '添加音轨…'
        selected = []
        w.audio.browse = lambda: selected.append(True)
        QTest.mouseClick(w.start, Qt.LeftButton)
        assert selected == [True] and w.worker is None, 'missing-audio action did not open audio selection'
    finally:
        main.find_tool = original_find


def dual_existing_atmos_bypasses_encoder(w):
    w.load_whole()
    quality, original = parsed_audio_fixture('eac3')
    try:
        ok, detail = w.drop([fixture / 'replacement.wav'], target=w.audio)
        assert ok, detail
        w.atmos_backend_status = lambda: {'available': False, 'message': 'missing encoder'}
        w.refresh_start_state()
        assert w.config()['audio_mode'] == 'atmos_auto' and w.config()['require_atmos']
        assert not w.atmos_preflight_error(w.config()), 'existing confirmed JOC unnecessarily requires an encoder'
        assert '保留已有 Atmos' in w.automatic_summary.text()
        assert w.top_status.text() == '可以处理' and w.start.text() == '开始处理'
        assert '准备就绪' in w.stage_label.text() and '可直接开始处理' in w.drop_hint.text()
        w.select_data(w.audio_mode, 'aac')
        assert w.atmos_preflight_error(w.config()), 'dual target accepted an ordinary codec replacement'
        assert w.top_status.text() == '请调整 Atmos 音频设置' and w.start.text() == '调整音频…'
    finally:
        quality.inspect_audio = original


def readiness_changes_with_encoder_and_export_target(w):
    # This UI fixture supplies configuration status only; it never claims that
    # the encoder is licensed or that new Atmos was actually produced.
    available = [False]
    w.atmos_backend_status = lambda: {'available': available[0], 'message': '本机编码设置尚未完成'}
    w.load_whole()
    assert w.top_status.text() == '请先配置 Atmos 编码器' and w.start.text() == '配置 Atmos…'
    available[0] = True
    w.atmos_settings_changed()
    assert w.top_status.text() == '可以处理' and w.start.text() == '开始处理'
    assert '准备就绪' in w.stage_label.text() and '可直接开始处理' in w.drop_hint.text()
    assert '请先配置' not in w.notification.text(), 'resolved prerequisite left a stale blocker'
    available[0] = False
    w.atmos_settings_changed()
    assert w.top_status.text() == '请先配置 Atmos 编码器'
    for target in ('preserve', 'quality', 'bili_make'):
        w.select_data(w.export_preset, target)
        assert not w.config()['require_atmos']
        assert w.top_status.text() == '可以处理' and w.start.text() == '开始处理'
        assert '准备就绪' in w.stage_label.text() and '可直接开始处理' in w.drop_hint.text()
    w.select_data(w.export_preset, 'bili_dual')
    assert w.top_status.text() == '请先配置 Atmos 编码器' and w.start.text() == '配置 Atmos…'


def cloud_settings_consent_is_per_session(w):
    QTest.mouseClick(w.atmos_setup, Qt.LeftButton)
    choose_combo_with_keys(w.atmos_backend, 'aws')
    assert w.aws_fields.isVisible() and not w.dee_fields.isVisible()
    assert not w.aws_allow.isChecked()
    values = {'region': 'ap-southeast-1', 'bucket': 'example-private-audio',
              'role_arn': 'arn:aws:iam::123456789012:role/MediaConvert',
              'profile': 'workstation', 'prefix': 'dolby-workstation'}
    for key, value in values.items():
        w.aws_inputs[key].setText(value)
    toggle_checkbox_with_space(w.aws_allow)
    assert w.config()['cloud_atmos']['allow_upload']
    w.save_preferences()
    saved = json.loads(w.settings_file.read_text(encoding='utf-8'))
    assert saved['cloud_atmos'] == dict(values, allow_upload=False)
    assert set(saved['cloud_atmos']) == set(values) | {'allow_upload'}, 'unexpected credential fields persisted'
    w.load_preferences()
    assert not w.aws_allow.isChecked(), 'cloud upload/charging was enabled from a saved preference'
    w.select_data(w.atmos_backend, 'dee')
    assert w.dee_fields.isVisible() and not w.aws_fields.isVisible()
    w.dee_path.set_file('C:/licensed-dee/encoder.exe')
    w.dee_profile.set_file('C:/licensed-dee/profile.json')
    assert w.config()['dee_profile'].endswith('profile.json')


def cloud_resume_requires_fresh_consent(w):
    w.load_whole()
    saved = dict(w.config(), atmos_backend='aws', cloud_atmos={
        'region': 'ap-southeast-1', 'bucket': 'example-private-audio',
        'role_arn': 'arn:aws:iam::123456789012:role/MediaConvert',
        'profile': 'workstation', 'prefix': 'dolby-workstation', 'allow_upload': True})
    captures = []
    w.begin = lambda cfg: captures.append(cfg)
    original = main.load_checkpoint
    main.load_checkpoint = lambda: {'cfg': saved}
    try:
        w.resume_previous()
        assert not captures and not w.aws_allow.isChecked(), 'checkpoint silently enabled cloud charges'
        assert w.aws_fields.isVisible() and w.advanced_toggle.isChecked()
        toggle_checkbox_with_space(w.aws_allow)
        w.resume_previous()
        assert captures and captures[0]['cloud_atmos']['allow_upload'] and captures[0]['resume']
        # A completed copy stage does not need a cloud upload grant. The engine
        # receives False and remains responsible for rechecking the real source.
        w.aws_allow.setChecked(False)
        main.load_checkpoint = lambda: {'cfg': saved, 'atmos_audio': {'generated': False, 'method': 'source_stream_copy'}}
        captures.clear()
        w.resume_previous()
        assert captures and not captures[0]['cloud_atmos']['allow_upload']
    finally:
        main.load_checkpoint = original


def preferences_migrate_only_legacy_dolby_defaults(w):
    version, saved_preset, expected_preset = PREFERENCE_CASES[case]
    assert w.export_preset.currentData() == expected_preset, 'startup did not respect the preference migration rule'
    cfg = w.config()
    migrated = version == 1 and saved_preset in ('bili_make', 'bili_preserve')
    if migrated:
        assert cfg['audio_mode'] == 'atmos_auto' and cfg['require_atmos'] and cfg['color_mode'] == 'dv84'
    else:
        assert cfg['audio_mode'] == 'aac' and not cfg['require_atmos'], 'explicit non-dual preferences were upgraded unexpectedly'
    assert w.crf.value() == 19, 'migration discarded unrelated preferences'
    w.load_whole()
    assert w.export_preset.currentData() == expected_preset, 'first import overrode the persisted or migrated target'
    if migrated:
        assert w.config()['audio_mode'] == 'atmos_auto' and w.config()['require_atmos']
    w.save_preferences()
    saved = json.loads(w.settings_file.read_text(encoding='utf-8'))
    assert saved['version'] == 2 and saved['export_preset'] == expected_preset
    restarted = original_window()
    try:
        assert restarted.export_preset.currentData() == expected_preset, 'saved v2 target did not survive reopening'
        assert restarted.config()['require_atmos'] == migrated
    finally:
        restarted.close()


def legacy_job_restore_does_not_migrate_target(w):
    w.load_whole()
    original = main.load_checkpoint
    saved = dict(w.config(), route='preserve', delivery='bilibili',
                 require_atmos=False, audio_mode='copy', color_mode='dv84')
    captures = []
    w.begin = lambda cfg: captures.append(cfg)
    main.load_checkpoint = lambda: {'cfg': saved}
    try:
        w.resume_previous()
        assert w.export_preset.currentData() == 'bili_preserve'
        assert w.config()['audio_mode'] == 'copy' and not w.config()['require_atmos']
        assert captures == [dict(saved, resume=True)], 'legacy task parameters were migrated with preferences'
    finally:
        main.load_checkpoint = original


scenarios = {
    'whole': whole_original, 'pair': paired_drop, 'audio_first': audio_first,
    'automatic_dolby': automatic_dolby_settings, 'missing_dolby': missing_dolby_tool_is_actionable,
    'source_transfer': explicit_source_dynamic_range,
    'audio_container': audio_only_container, 'restore': replace_and_restore,
    'audio_from_video': audio_from_video_section, 'multiple_video': reject_multiple_video,
    'invalid_atomic': reject_invalid_atomically, 'output': automatic_and_user_output,
    'async': asynchronous_validation, 'processing': processing_locks_intake,
    'maximized': maximized_window_controls, 'export': paired_drop_finishes_export,
    'export_failure': failed_export_keeps_ui_usable,
    'atmos_modes': atmos_mode_notice, 'truehd_notice': truehd_conversion_notice,
    'native_compatibility': unsupported_copy_gets_native_export,
    'transport_compatibility': transport_stream_gets_native_export,
    'same_source_settings': same_source_keeps_manual_picture_settings,
    'restored_picture': restored_picture_survives_inspection,
    'dual_missing': dual_target_blocks_missing_encoder,
    'dual_existing': dual_existing_atmos_bypasses_encoder,
    'readiness': readiness_changes_with_encoder_and_export_target,
    'cloud_settings': cloud_settings_consent_is_per_session,
    'cloud_resume': cloud_resume_requires_fresh_consent,
    'legacy_job': legacy_job_restore_does_not_migrate_target,
}
scenarios.update({name: preferences_migrate_only_legacy_dolby_defaults for name in PREFERENCE_CASES})
main.MainWindow = IntakeWindow
result = main.main()
sys.exit(1 if failed else result)
'''


@unittest.skipUnless(importlib.util.find_spec("PyQt5") and FFMPEG and FFPROBE, "PyQt5 and FFmpeg required")
class IntakeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="dolby-intake-")
        cls.fixture = Path(cls.temp.name).resolve()
        # Keep the real Qt child isolated without exceeding Windows' 32K
        # command-line limit as drag/drop scenarios accumulate.
        cls.child_script = cls.fixture / "intake_scenarios.py"
        cls.child_script.write_text(f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n" + CHILD, encoding="utf-8")
        def command(*args):
            result = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=30)
            if result.returncode:
                raise AssertionError(result.stderr[-3000:])
        command(FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=192x128:rate=12:duration=0.5",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", cls.fixture / "whole original.mp4")
        command(FFMPEG, "-v", "error", "-i", cls.fixture / "whole original.mp4", "-map", "0:v:0", "-c", "copy",
                "-an", cls.fixture / "video only.mp4")
        shutil.copy2(cls.fixture / "video only.mp4", cls.fixture / "second video.mp4")
        command(FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=0.5",
                cls.fixture / "replacement.wav")
        command(FFMPEG, "-v", "error", "-i", cls.fixture / "replacement.wav", "-c:a", "aac", "-vn",
                cls.fixture / "audio inside.mp4")
        command(FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=192x128:rate=6:duration=1",
                "-c:v", "ffv1", "-level", "3", "-pix_fmt", "yuv420p10le", "-color_primaries", "bt709",
                "-color_trc", "bt709", "-colorspace", "bt709", cls.fixture / "lossless FFV1.mkv")
        command(FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=192x128:rate=6:duration=1",
                "-frames:v", "6", "-c:v", "libx264", "-bf", "0", "-pix_fmt", "yuv420p", "-color_primaries", "bt709",
                "-color_trc", "bt709", "-colorspace", "bt709", "-f", "mpegts", cls.fixture / "broadcast H264.ts")
        (cls.fixture / "broken.mp4").write_text("not a video", encoding="utf-8")
        (cls.fixture / "broken.wav").write_text("not an audio stream", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def scenario(self, name):
        result = subprocess.run([sys.executable, str(self.child_script), str(self.fixture), name], cwd=ROOT,
                                env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1"),
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr[-6000:] + result.stdout[-2000:])
        self.assertIn("INTAKE_UI_OK:" + name, result.stdout)

    def test_whole_clip_drop_keeps_original_audio_and_prepares_output(self):
        self.scenario("whole")

    def test_dual_target_blocks_missing_encoder_and_missing_audio(self):
        self.scenario("dual_missing")

    def test_dual_target_preserves_confirmed_atmos_without_encoder(self):
        self.scenario("dual_existing")

    def test_readiness_tracks_configuration_and_leaves_other_targets_usable(self):
        self.scenario("readiness")

    def test_cloud_settings_never_restore_upload_and_charging_consent(self):
        self.scenario("cloud_settings")

    def test_resuming_cloud_task_requires_this_session_consent(self):
        self.scenario("cloud_resume")

    def test_legacy_dolby_preferences_upgrade_to_double_dolby_on_first_start(self):
        for case in ("prefs_v1_make", "prefs_v1_bili_preserve"):
            with self.subTest(case=case):
                self.scenario(case)

    def test_legacy_non_dolby_and_explicit_v2_targets_survive_start_and_import(self):
        for case in ("prefs_v1_preserve", "prefs_v1_quality", "prefs_v1_enhance",
                     "prefs_v2_make", "prefs_v2_bili_preserve"):
            with self.subTest(case=case):
                self.scenario(case)

    def test_restoring_legacy_job_keeps_its_original_single_dolby_target(self):
        self.scenario("legacy_job")

    def test_automatic_dolby_presets_limits_metadata_and_manual_controls(self):
        self.scenario("automatic_dolby")

    def test_missing_dolby_tool_explains_installation_without_degraded_output(self):
        self.scenario("missing_dolby")

    def test_unresolved_bt2020_requires_explicit_source_dynamic_range(self):
        self.scenario("source_transfer")

    def test_same_drop_routes_separate_video_and_audio_independent_of_order(self):
        self.scenario("pair")

    def test_audio_dropped_first_survives_later_video_intake(self):
        self.scenario("audio_first")

    def test_mp4_with_only_audio_is_classified_by_streams(self):
        self.scenario("audio_container")

    def test_audio_section_replacement_and_restore_original_button(self):
        self.scenario("restore")

    def test_audio_section_can_extract_audio_from_a_video_container(self):
        self.scenario("audio_from_video")

    def test_multiple_video_drop_explains_rejection_and_keeps_source(self):
        self.scenario("multiple_video")

    def test_invalid_single_or_paired_drop_is_atomic(self):
        self.scenario("invalid_atomic")

    def test_auto_output_updates_but_keyboard_selected_output_is_preserved(self):
        self.scenario("output")

    def test_same_video_or_video_with_new_audio_keeps_manual_picture_settings(self):
        self.scenario("same_source_settings")

    def test_restored_picture_parameters_survive_async_inspection_and_reimport(self):
        self.scenario("restored_picture")

    def test_real_inspection_does_not_block_gui_event_loop(self):
        self.scenario("async")

    def test_running_worker_locks_drag_intake_and_unlocks_after_stop(self):
        self.scenario("processing")

    def test_failed_export_explains_actual_error_and_allows_another_task(self):
        self.scenario("export_failure")

    @unittest.skipUnless(DOVI, "dovi_tool required for automatic Dolby export")
    def test_paired_drop_can_finish_export_without_manual_codec_adjustment(self):
        self.scenario("export")

    @unittest.skipUnless(DOVI, "dovi_tool required for automatic Dolby export")
    def test_lossless_mkv_drop_automatically_finishes_compatible_native_export(self):
        self.scenario("native_compatibility")

    @unittest.skipUnless(DOVI, "dovi_tool required for automatic Dolby export")
    def test_h264_transport_stream_drop_automatically_finishes_compatible_native_export(self):
        self.scenario("transport_compatibility")

    def test_actual_startup_is_maximized_and_normal_window_controls_remain_available(self):
        self.scenario("maximized")

    def test_audio_controls_explain_atmos_loss_and_preserve_restored_choices(self):
        self.scenario("atmos_modes")
        self.scenario("truehd_notice")


if __name__ == "__main__":
    unittest.main()
