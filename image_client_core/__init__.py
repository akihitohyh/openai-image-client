"""Logic shared by the web app (``web_app.py``) and the CLI (``generate_image.py``).

Both entry points used to carry byte-identical copies of these helpers, and fixes
routinely landed in only one of them. Anything that is pure, or depends only on a
``requests.Response``, belongs here so there is exactly one implementation.

Only stdlib and ``requests`` may be imported from this package — it must stay
usable by the standalone web app, whose only dependency is ``requests``.
"""
