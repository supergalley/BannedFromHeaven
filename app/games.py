"""Logged-in arcade and replay-verified, per-player leaderboards."""
import hashlib
import json
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, abort, jsonify, render_template, request, session, make_response
from flask_login import current_user, login_required
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from . import db
from .arcade.rules import FPS, MAX_FRAMES, RULES_VERSION, ReplayError, replay_score, validate_trace
from .models import ArcadeRun, ArcadeBestScore, User


ASSET_VERSION = 4

games_bp = Blueprint("games", __name__, url_prefix="/games")
GAMES = {
    "flappy-devil": dict(name="Flappy Devil", genre="Flap. Dodge. Repeat.",
                        description="A little devil, a lot of bad decisions. Squeeze through the gates of hell.",
                        controls="Tap the game, the Flap button, or Space to flap. One point per gate.", accent="#ff706e"),
    "spunk-invaders": dict(name="Spunk Invaders", genre="The eggs are coming.",
                          description="Launch swimming tadpoles. Turn invading eggs into fading little embryos.",
                          controls="Move with ← → or A/D. Hold Space or Fire to shoot. 10 points per egg; 25 per cleared wave.", accent="#a895ff"),
    "hell-serpent": dict(name="Hell Serpent", genre="One more soul.",
                         description="Collect glowing souls, grow your serpent, and keep clear of your own tail.",
                         controls="Use arrow keys, WASD, or the direction buttons. Collect a soul for 10 points.", accent="#a5ef8d"),
}


def _game(slug):
    if slug not in GAMES:
        abort(404)
    return GAMES[slug]


def _csrf_required():
    saved = session.get("arcade_csrf")
    if not saved or not secrets.compare_digest(saved, request.headers.get("X-Arcade-CSRF", "")):
        abort(403)


def _json_error(message, status=400):
    response = jsonify(error=message)
    response.headers["Cache-Control"] = "no-store"
    return response, status


def _board(slug):
    query = ArcadeBestScore.query.filter_by(game_slug=slug, rules_version=RULES_VERSION)
    leaders = query.join(User).order_by(
        ArcadeBestScore.score.desc(), ArcadeBestScore.achieved_at.asc(), ArcadeBestScore.id.asc()
    ).limit(10).all()
    mine = query.filter_by(user_id=current_user.id).first()
    rank = None
    if mine:
        rank = query.filter(or_(
            ArcadeBestScore.score > mine.score,
            and_(ArcadeBestScore.score == mine.score, ArcadeBestScore.achieved_at < mine.achieved_at),
            and_(ArcadeBestScore.score == mine.score, ArcadeBestScore.achieved_at == mine.achieved_at,
                 ArcadeBestScore.id < mine.id),
        )).count() + 1
    return dict(
        leaderboard_html=render_template("games_leaderboard.html", leaders=leaders),
        personal_best=mine.score if mine else 0, rank=rank,
    )


@games_bp.route("")
@games_bp.route("/")
@login_required
def index():
    selected = request.args.get("game", "flappy-devil")
    if selected not in GAMES:
        selected = "flappy-devil"
    if not session.get("arcade_csrf"):
        session["arcade_csrf"] = secrets.token_urlsafe(32)
    bests = {row.game_slug: row.score for row in ArcadeBestScore.query.filter_by(
        user_id=current_user.id, rules_version=RULES_VERSION).all()}
    response = make_response(render_template(
        "games.html", games=GAMES, selected=selected,
        personal_bests=bests, board=_board(selected), csrf=session["arcade_csrf"],
        fps=FPS, max_frames=MAX_FRAMES, rules_version=RULES_VERSION, asset_version=ASSET_VERSION))
    response.headers["Cache-Control"] = "private, no-store"
    return response


@games_bp.route("/<slug>/leaderboard")
@login_required
def leaderboard(slug):
    _game(slug)
    response = jsonify(_board(slug))
    response.headers["Cache-Control"] = "no-store"
    return response


@games_bp.route("/<slug>/start", methods=["POST"])
@login_required
def start(slug):
    _game(slug)
    _csrf_required()
    now = datetime.utcnow()
    if ArcadeRun.query.filter(ArcadeRun.user_id == current_user.id,
                              ArcadeRun.started_at >= now - timedelta(minutes=1)).count() >= 30:
        return _json_error("Too many new rounds. Give it a moment and try again.", 429)
    ArcadeRun.query.filter_by(user_id=current_user.id, game_slug=slug, consumed_at=None).update(
        {ArcadeRun.consumed_at: now}, synchronize_session=False)
    run = ArcadeRun(id=secrets.token_urlsafe(24), user_id=current_user.id, game_slug=slug,
                    seed=secrets.randbits(32), started_at=now, rules_version=RULES_VERSION)
    db.session.add(run)
    db.session.commit()
    response = jsonify(run_id=run.id, seed=run.seed, fps=FPS, max_frames=MAX_FRAMES, rules_version=RULES_VERSION)
    response.headers["Cache-Control"] = "no-store"
    return response


@games_bp.route("/<slug>/score", methods=["POST"])
@login_required
def submit_score(slug):
    _game(slug)
    _csrf_required()
    request.max_content_length = 512 * 1024
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("run_id"), str):
        return _json_error("Missing round details.")
    run = ArcadeRun.query.filter_by(id=data["run_id"], user_id=current_user.id, game_slug=slug).first_or_404()
    if run.rules_version != RULES_VERSION:
        return _json_error("This round uses an older version of the game. Start a fresh round.", 409)
    frames, events = data.get("frames"), data.get("events")
    try:
        validate_trace(slug, frames, events)
    except ReplayError as exc:
        return _json_error(str(exc))
    trace_hash = hashlib.sha256(json.dumps([frames, events], separators=(",", ":")).encode()).hexdigest()
    claimed = data.get("score")
    if type(claimed) is not int or claimed < 0:
        return _json_error("Invalid score.")
    if run.consumed_at:
        if run.trace_hash == trace_hash and run.verified_score == claimed:
            response = jsonify(saved=True, score=run.verified_score, new_best=run.was_personal_best, **_board(slug))
            response.headers["Cache-Control"] = "no-store"
            return response
        return _json_error("This round is already closed. Start a new round.", 409)
    now = datetime.utcnow()
    elapsed = (now - run.started_at).total_seconds()
    if elapsed > 30 * 60:
        return _json_error("This round expired. Start a new round.", 409)
    if elapsed < -2 or elapsed + 2 < frames / FPS:
        return _json_error("The replay is longer than the time spent playing.")
    try:
        verified = replay_score(slug, run.seed, frames, events)
    except ReplayError as exc:
        return _json_error(str(exc))
    if verified != claimed:
        return _json_error("The score did not match the replay. Refresh the page before your next round.")
    claimed_row = ArcadeRun.query.filter_by(id=run.id, user_id=current_user.id, consumed_at=None).update(
        {ArcadeRun.consumed_at: now, ArcadeRun.verified_score: verified,
         ArcadeRun.frames: frames, ArcadeRun.trace_hash: trace_hash}, synchronize_session="fetch")
    if not claimed_row:
        db.session.rollback()
        return _json_error("This round was already saved or replaced by a newer round.", 409)
    best = ArcadeBestScore.query.filter_by(user_id=current_user.id, game_slug=slug,
                                          rules_version=RULES_VERSION).first()
    new_best = verified > 0 and (best is None or verified > best.score)
    if new_best:
        if best is None:
            best = ArcadeBestScore(user_id=current_user.id, game_slug=slug, rules_version=RULES_VERSION)
            db.session.add(best)
        best.score, best.achieved_at, best.run_id = verified, now, run.id
    run.was_personal_best = new_best
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return _json_error("A score changed in another tab. Please retry saving.", 409)
    response = jsonify(saved=True, score=verified, new_best=new_best, **_board(slug))
    response.headers["Cache-Control"] = "no-store"
    return response
