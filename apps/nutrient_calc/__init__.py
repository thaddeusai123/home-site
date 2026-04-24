"""
Nutrient Calculator — Masterblend 4-18-38 + Calcium Nitrate + Epsom
mixing calculator with live NPK ppm readout and a pH-down estimator.
All math is client-side so every input is instantly reactive.
"""

from __future__ import annotations

from flask import Blueprint, render_template

bp = Blueprint("nutrient_calc", __name__, url_prefix="/nutrient-calc")

APP_META = {
    "slug": "nutrient-calc",
    "name": "Nutrient Calculator",
    "tagline": "Masterblend NPK mixer + pH down estimator.",
    "icon": "\U0001f9ea",
    "url_endpoint": "nutrient_calc.index",
    "status_endpoint": None,
}


@bp.route("/")
def index():
    return render_template("nutrient_calc/index.html")
