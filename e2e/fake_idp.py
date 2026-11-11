"""A tiny OpenID Connect provider for end-to-end tests (never for real use).

It speaks the real protocol (discovery, authorization code with PKCE and nonce, signed RS256 id_token,
JWKS), so the application's genuine Authlib sign-in code runs against it. It signs in whoever the test
last selected through POST /_login.
"""
import base64
import hashlib
import secrets
import sys
import time

from authlib.jose import JsonWebKey, jwt
from flask import Flask, jsonify, redirect, request

PORT = int(sys.argv[1])
BASE = f"http://127.0.0.1:{PORT}"
KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "test-key"})
app = Flask(__name__)
who = {"email": "ed@corp.example", "name": "Ed Editor", "email_verified": True}
codes = {}


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/.well-known/openid-configuration")
def metadata():
    return jsonify(
        issuer=BASE, authorization_endpoint=BASE + "/authorize", token_endpoint=BASE + "/token", jwks_uri=BASE + "/jwks",
        response_types_supported=["code"], subject_types_supported=["public"],
        id_token_signing_alg_values_supported=["RS256"], code_challenge_methods_supported=["S256"],
    )


@app.get("/jwks")
def jwks():
    return jsonify({"keys": [KEY.as_dict(is_private=False)]})


@app.post("/_login")
def choose_user():
    who.update(request.get_json())
    return jsonify(who)


@app.get("/authorize")
def authorize():
    args = request.args
    code = secrets.token_urlsafe(16)
    codes[code] = {"nonce": args.get("nonce"), "challenge": args.get("code_challenge"), "client_id": args["client_id"],
                   "redirect_uri": args["redirect_uri"], "who": dict(who)}
    return redirect(f"{args['redirect_uri']}?code={code}&state={args['state']}")


@app.post("/token")
def token():
    form = request.form
    entry = codes.pop(form.get("code", ""), None)
    if not entry or entry["redirect_uri"] != form.get("redirect_uri"):
        return jsonify(error="invalid_grant"), 400
    digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
    if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != entry["challenge"]:
        return jsonify(error="invalid_grant", error_description="PKCE verification failed"), 400
    now = int(time.time())
    claims = {"iss": BASE, "sub": entry["who"]["email"], "aud": entry["client_id"], "iat": now, "exp": now + 300,
              "nonce": entry["nonce"], **entry["who"]}
    id_token = jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, KEY).decode()
    return jsonify(access_token=secrets.token_urlsafe(16), token_type="Bearer", expires_in=300, id_token=id_token)


if __name__ == "__main__":
    from waitress import serve

    serve(app, host="127.0.0.1", port=PORT)
