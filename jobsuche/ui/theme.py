"""Farben und Stylesheet, angelehnt an AI-Desc."""

from pathlib import Path

DUNKEL = {
    "bg": "#1a1c22", "sidebar": "#1c1e24", "karte": "#252833", "feld": "#1d1f26", "rand": "#383b47",
    "text": "#e7e8ec", "gedimmt": "#9a9eab", "akzent": "#f5a623", "aktiv": "#2c303b",
    "ok_bg": "#1c3b30", "ok": "#3ecf8e", "warn_bg": "#3f2a12", "warn": "#f5a623", "fehler": "#f0716a",
    "auf_akzent": "#1a1c22",
}
HELL = {
    "bg": "#f4f5f7", "sidebar": "#ebedf1", "karte": "#ffffff", "feld": "#f7f8fa", "rand": "#d5d8df",
    "text": "#1f2229", "gedimmt": "#5f6470", "akzent": "#b86e00", "aktiv": "#dcdfe6",
    "ok_bg": "#d9f2e5", "ok": "#116b43", "warn_bg": "#fdebd0", "warn": "#8a5200", "fehler": "#b3261e",
    "auf_akzent": "#ffffff",
}


AKTUELL = DUNKEL  # Für Links in Rich Text, die das Stylesheet nicht erreicht.


# Qt zeichnet bei Zahlenfeldern keine CSS-Dreiecke wie bei Auswahllisten: zwei kleine SVG-Pfeile, grau für beide Themes
PFEIL_HOCH = (Path(__file__).parent / "pfeil_hoch.svg").as_posix()
PFEIL_RUNTER = (Path(__file__).parent / "pfeil_runter.svg").as_posix()


def stylesheet(t: dict) -> str:
    global AKTUELL
    AKTUELL = t
    return f"""
    QWidget {{ color: {t['text']}; font-size: 13px; }}
    QMainWindow, #inhalt, QScrollArea, #scrollinhalt {{ background: {t['bg']}; border: none; }}
    #sidebar {{ background: {t['sidebar']}; border-right: 1px solid {t['rand']}; }}
    #logo {{ font-size: 20px; font-weight: 700; }}
    #gedimmt, #sidebarUnter {{ color: {t['gedimmt']}; }}
    #navSektion {{ color: {t['gedimmt']}; font-size: 11px; font-weight: 700; padding: 12px 0 4px 14px; }}
    #navEintrag {{ background: transparent; border: none; border-radius: 8px; padding: 9px 14px; text-align: left; }}
    #navEintrag:hover {{ background: {t['aktiv']}; }}
    #navEintrag:checked {{ background: {t['aktiv']}; color: {t['akzent']}; }}
    #fussProfil {{ background: transparent; border: none; color: {t['gedimmt']}; padding: 0 0 0 6px; text-align: left; }}
    #fussProfil:hover {{ color: {t['akzent']}; }}
    #avatar {{ background: {t['akzent']}; color: {t['auf_akzent']}; border-radius: 17px; font-weight: 700; }}
    #titel {{ font-size: 21px; font-weight: 700; }}
    #karte {{ background: {t['karte']}; border: 1px solid {t['rand']}; border-radius: 12px; }}
    #wohnungvorschau {{ background: {t['bg']}; border: 1px solid {t['rand']}; border-radius: 10px; }}
    #karteTitel {{ font-size: 14px; font-weight: 700; }}
    #pill {{ background: {t['ok_bg']}; color: {t['ok']}; border-radius: 13px; padding: 5px 16px; }}
    #pillBeschaeftigt {{ background: {t['warn_bg']}; color: {t['warn']}; border-radius: 13px; padding: 5px 16px; }}
    #themaKnopf {{ background: transparent; border: none; color: {t['gedimmt']}; padding: 6px 10px; }}
    #sperre {{ color: {t['fehler']}; }} #hinweis {{ color: {t['warn']}; }} #okText {{ color: {t['ok']}; }}
    QPushButton {{ background: transparent; border: 1px solid {t['rand']}; border-radius: 8px; padding: 7px 14px; }}
    QPushButton:hover {{ background: {t['aktiv']}; }}
    QPushButton:disabled {{ color: {t['gedimmt']}; }}
    #primaer {{ border: 1px solid {t['akzent']}; color: {t['akzent']}; }}
    #primaer:disabled {{ border: 1px solid {t['rand']}; color: {t['gedimmt']}; }}
    Schalter {{
        qproperty-farbeAn: {t['akzent']}; qproperty-farbeAus: {t['rand']}; qproperty-farbeKnopf: {t['auf_akzent']};
        qproperty-farbeSchrift: {t['text']}; qproperty-farbeGedimmt: {t['gedimmt']};
    }}
    QDialog {{ background: {t['karte']}; }}
    QProgressBar {{ background: {t['feld']}; border: 1px solid {t['rand']}; border-radius: 6px; text-align: center; height: 16px; }}
    QProgressBar::chunk {{ background: {t['akzent']}; border-radius: 5px; }}
    QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{
        background: {t['feld']}; border: 1px solid {t['rand']}; border-radius: 8px; padding: 7px 10px;
        selection-background-color: {t['akzent']}; selection-color: {t['auf_akzent']};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {t['akzent']}; }}
    QSpinBox {{ color: {t['text']}; padding-right: 22px; }}
    QSpinBox::up-button, QSpinBox::down-button {{ border: none; width: 20px; height: 12px; background: transparent; }}
    QSpinBox::up-button {{ subcontrol-position: top right; margin-top: 4px; }}
    QSpinBox::down-button {{ subcontrol-position: bottom right; margin-bottom: 4px; }}
    QSpinBox::up-arrow {{ image: url({PFEIL_HOCH}); width: 8px; height: 5px; }}
    QSpinBox::down-arrow {{ image: url({PFEIL_RUNTER}); width: 8px; height: 5px; }}
    QComboBox QAbstractItemView {{ background: {t['karte']}; selection-background-color: {t['aktiv']}; }}
    QTableWidget, QListWidget {{
        background: transparent; border: none; gridline-color: transparent; outline: none;
        alternate-background-color: transparent;
    }}
    QTableWidget::item, QListWidget::item {{ padding: 7px 2px; border-bottom: 1px solid {t['rand']}; }}
    QTableWidget::item:selected, QListWidget::item:selected {{ background: {t['aktiv']}; color: {t['akzent']}; }}
    QHeaderView {{ background: transparent; }}
    QHeaderView::section {{
        background: transparent; color: {t['gedimmt']}; border: none; border-bottom: 1px solid {t['rand']};
        padding: 6px 8px; font-weight: 700;
    }}
    QTableCornerButton::section {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 9px; }}
    QScrollBar::handle:vertical {{ background: {t['rand']}; border-radius: 4px; min-height: 24px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 9px; }}
    QScrollBar::handle:horizontal {{ background: {t['rand']}; border-radius: 4px; min-width: 24px; }}
    QMessageBox, QInputDialog {{ background: {t['karte']}; }}
    QToolTip {{ background: {t['karte']}; color: {t['text']}; border: 1px solid {t['rand']}; }}
    """
