from datetime import datetime, timezone
from flask import (
    render_template,
    flash,
    redirect,
    url_for,
    request,
    g,
    current_app,
    send_file,
)
from flask_login import current_user, login_required
from flask_babel import _, get_locale
import sqlalchemy as sa
from langdetect import detect, LangDetectException
from app import db
from app.main.forms import EditProfileForm, EmptyForm, PostForm, SearchForm, MessageForm
from app.models import User, Post, Message, Notification
from app.translate import translate
from app.main import bp

from opentimestamps.core.timestamp import DetachedTimestampFile
from opentimestamps.core.op import OpAppend

from werkzeug.utils import secure_filename
import os

ALLOWED_EXTENSIONS = {"pdf", "txt", "docx", "jpg"}


# Check allowed file extension
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@bp.before_app_request
def before_request():
    if current_user.is_authenticated:
        current_user.last_seen = datetime.now(timezone.utc)
        db.session.commit()
        g.search_form = SearchForm()
    g.locale = str(get_locale())


@bp.route("/", methods=["GET", "POST"])
@bp.route("/index", methods=["GET", "POST"])
@login_required
def index():
    form = PostForm()
    if form.validate_on_submit():
        try:
            language = detect(form.post.data)
        except LangDetectException:
            language = ""
        post = Post(body=form.post.data, author=current_user, language=language)
        db.session.add(post)
        db.session.commit()
        flash(_("Your post is now live!"))
        return redirect(url_for("main.index"))
    page = request.args.get("page", 1, type=int)
    posts = db.paginate(
        current_user.following_posts(),
        page=page,
        per_page=current_app.config["POSTS_PER_PAGE"],
        error_out=False,
    )
    next_url = url_for("main.index", page=posts.next_num) if posts.has_next else None
    prev_url = url_for("main.index", page=posts.prev_num) if posts.has_prev else None
    return render_template(
        "index.html",
        title=_("Home"),
        form=form,
        posts=posts.items,
        next_url=next_url,
        prev_url=prev_url,
    )


@bp.route("/explore")
@login_required
def explore():
    page = request.args.get("page", 1, type=int)
    query = sa.select(Post).order_by(Post.timestamp.desc())
    posts = db.paginate(
        query, page=page, per_page=current_app.config["POSTS_PER_PAGE"], error_out=False
    )
    next_url = url_for("main.explore", page=posts.next_num) if posts.has_next else None
    prev_url = url_for("main.explore", page=posts.prev_num) if posts.has_prev else None
    return render_template(
        "index.html",
        title=_("Explore"),
        posts=posts.items,
        next_url=next_url,
        prev_url=prev_url,
    )


@bp.route("/user/<username>")
@login_required
def user(username):
    user = db.first_or_404(sa.select(User).where(User.username == username))
    page = request.args.get("page", 1, type=int)
    query = user.posts.select().order_by(Post.timestamp.desc())
    posts = db.paginate(
        query, page=page, per_page=current_app.config["POSTS_PER_PAGE"], error_out=False
    )
    next_url = (
        url_for("main.user", username=user.username, page=posts.next_num)
        if posts.has_next
        else None
    )
    prev_url = (
        url_for("main.user", username=user.username, page=posts.prev_num)
        if posts.has_prev
        else None
    )
    form = EmptyForm()
    return render_template(
        "user.html",
        user=user,
        posts=posts.items,
        next_url=next_url,
        prev_url=prev_url,
        form=form,
    )


@bp.route("/user/<username>/popup")
@login_required
def user_popup(username):
    user = db.first_or_404(sa.select(User).where(User.username == username))
    form = EmptyForm()
    return render_template("user_popup.html", user=user, form=form)


@bp.route("/edit_profile", methods=["GET", "POST"])
@login_required
def edit_profile():
    form = EditProfileForm(current_user.username)
    if form.validate_on_submit():
        current_user.username = form.username.data
        current_user.about_me = form.about_me.data
        db.session.commit()
        flash(_("Your changes have been saved."))
        return redirect(url_for("main.edit_profile"))
    elif request.method == "GET":
        form.username.data = current_user.username
        form.about_me.data = current_user.about_me
    return render_template("edit_profile.html", title=_("Edit Profile"), form=form)


@bp.route("/follow/<username>", methods=["POST"])
@login_required
def follow(username):
    form = EmptyForm()
    if form.validate_on_submit():
        user = db.session.scalar(sa.select(User).where(User.username == username))
        if user is None:
            flash(_("User %(username)s not found.", username=username))
            return redirect(url_for("main.index"))
        if user == current_user:
            flash(_("You cannot follow yourself!"))
            return redirect(url_for("main.user", username=username))
        current_user.follow(user)
        db.session.commit()
        flash(_("You are following %(username)s!", username=username))
        return redirect(url_for("main.user", username=username))
    else:
        return redirect(url_for("main.index"))


@bp.route("/unfollow/<username>", methods=["POST"])
@login_required
def unfollow(username):
    form = EmptyForm()
    if form.validate_on_submit():
        user = db.session.scalar(sa.select(User).where(User.username == username))
        if user is None:
            flash(_("User %(username)s not found.", username=username))
            return redirect(url_for("main.index"))
        if user == current_user:
            flash(_("You cannot unfollow yourself!"))
            return redirect(url_for("main.user", username=username))
        current_user.unfollow(user)
        db.session.commit()
        flash(_("You are not following %(username)s.", username=username))
        return redirect(url_for("main.user", username=username))
    else:
        return redirect(url_for("main.index"))


@bp.route("/translate", methods=["POST"])
@login_required
def translate_text():
    data = request.get_json()
    return {
        "text": translate(data["text"], data["source_language"], data["dest_language"])
    }


@bp.route("/search")
@login_required
def search():
    if not g.search_form.validate():
        return redirect(url_for("main.explore"))
    page = request.args.get("page", 1, type=int)
    posts, total = Post.search(
        g.search_form.q.data, page, current_app.config["POSTS_PER_PAGE"]
    )
    next_url = (
        url_for("main.search", q=g.search_form.q.data, page=page + 1)
        if total > page * current_app.config["POSTS_PER_PAGE"]
        else None
    )
    prev_url = (
        url_for("main.search", q=g.search_form.q.data, page=page - 1)
        if page > 1
        else None
    )
    return render_template(
        "search.html",
        title=_("Search"),
        posts=posts,
        next_url=next_url,
        prev_url=prev_url,
    )


@bp.route("/send_message/<recipient>", methods=["GET", "POST"])
@login_required
def send_message(recipient):
    user = db.first_or_404(sa.select(User).where(User.username == recipient))
    form = MessageForm()
    if form.validate_on_submit():
        msg = Message(author=current_user, recipient=user, body=form.message.data)
        db.session.add(msg)
        user.add_notification("unread_message_count", user.unread_message_count())
        db.session.commit()
        flash(_("Your message has been sent."))
        return redirect(url_for("main.user", username=recipient))
    return render_template(
        "send_message.html", title=_("Send Message"), form=form, recipient=recipient
    )


@bp.route("/messages")
@login_required
def messages():
    current_user.last_message_read_time = datetime.now(timezone.utc)
    current_user.add_notification("unread_message_count", 0)
    db.session.commit()
    page = request.args.get("page", 1, type=int)
    query = current_user.messages_received.select().order_by(Message.timestamp.desc())
    messages = db.paginate(
        query, page=page, per_page=current_app.config["POSTS_PER_PAGE"], error_out=False
    )
    next_url = (
        url_for("main.messages", page=messages.next_num) if messages.has_next else None
    )
    prev_url = (
        url_for("main.messages", page=messages.prev_num) if messages.has_prev else None
    )
    return render_template(
        "messages.html", messages=messages.items, next_url=next_url, prev_url=prev_url
    )


@bp.route("/export_posts")
@login_required
def export_posts():
    if current_user.get_task_in_progress("export_posts"):
        flash(_("An export task is currently in progress"))
    else:
        current_user.launch_task("export_posts", _("Exporting posts..."))
        db.session.commit()
    return redirect(url_for("main.user", username=current_user.username))


@bp.route("/notifications")
@login_required
def notifications():
    since = request.args.get("since", 0.0, type=float)
    query = (
        current_user.notifications.select()
        .where(Notification.timestamp > since)
        .order_by(Notification.timestamp.asc())
    )
    notifications = db.session.scalars(query)
    return [
        {"name": n.name, "data": n.get_data(), "timestamp": n.timestamp}
        for n in notifications
    ]


import tempfile


# @bp.route("/stamp", methods=["GET", "POST"])
# @login_required
# def stamp():
#     if request.method == "POST":
#         if "file" not in request.files:
#             flash("No file part")
#             return redirect(request.url)

#         file = request.files["file"]
#         if file.filename == "":
#             flash("No selected file")
#             return redirect(request.url)

#         if file and allowed_file(file.filename):
#             filename = secure_filename(file.filename)

#             # Use a temporary directory
#             with tempfile.TemporaryDirectory() as temp_dir:
#                 file_path = os.path.join(temp_dir, filename)
#                 file.save(file_path)

#                 ots_file_path = file_path + ".ots"

#                 try:
#                     # Create a DetachedTimestampFile and apply an operation
#                     with open(file_path, "rb") as f:
#                         file_bytes = f.read()

#                     detached_timestamp = DetachedTimestampFile()
#                     detached_timestamp.file_bytes = file_bytes
#                     detached_timestamp.ops.append(OpAppend())

#                     # Serialize the timestamp
#                     timestamp_bytes = detached_timestamp.serialize()

#                     # Save the .ots file
#                     with open(ots_file_path, "wb") as f:
#                         f.write(timestamp_bytes)

#                     flash(
#                         "File stamped successfully! You can download the OTS file below."
#                     )
#                     return send_file(ots_file_path, as_attachment=True)

#                 except Exception as e:
#                     flash(f"Error stamping the file: {str(e)}")
#                     return redirect(request.url)

#     return render_template("stamp.html")

from flask import Flask, render_template, request, flash, send_file
from werkzeug.utils import secure_filename
import os

from opentimestamps.core.timestamp import Timestamp
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.timestamp import DetachedTimestampFile


# Function to stamp the file using OpenTimestamps
def stamp_file(file_path):
    try:
        # Open the file and create a SHA256 hash of the file contents
        with open(file_path, "rb") as f:
            file_hash_op = OpSHA256()
            timestamp = Timestamp(f.read())

        # Create a DetachedTimestampFile with the timestamp
        detached_timestamp = DetachedTimestampFile(file_hash_op, timestamp)

        # Define the file path for the timestamp .ots file
        timestamp_file_path = file_path + ".ots"

        # Save the timestamp to a file
        with open(timestamp_file_path, "wb") as f:
            detached_timestamp.serialize(f)

        return timestamp_file_path

    except Exception as e:
        flash(f"Error stamping the file: {str(e)}")


# Flask route for file stamping
@bp.route("/stamp", methods=["GET", "POST"])
def stamp():
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file part")
            return redirect(request.url)

        file = request.files["file"]
        if file.filename == "":
            flash("No selected file")
            return redirect(request.url)

        if file:
            # Use tempfile to create a temporary directory
            with tempfile.TemporaryDirectory() as temp_dir:
                # Create a valid file path by joining the temp directory with the secure filename
                filename = secure_filename(file.filename)
                file_path = os.path.join(temp_dir, filename)
                file.save(file_path)

                # Call the function to stamp the file
                stamped_file_path = stamp_file(file_path)

                if stamped_file_path:
                    return send_file(stamped_file_path, as_attachment=True)
                else:
                    flash("Error stamping the file.")
                    return redirect(request.url)

    # Ensure the route always renders the template for GET requests or after POST if no errors
    return render_template("stamp.html")
