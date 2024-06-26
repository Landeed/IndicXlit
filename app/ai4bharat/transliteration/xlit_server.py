"""
Expose Transliteration Engine as an HTTP API.

USAGE:
```
from ai4bharat.transliteration import xlit_server
app, engine = xlit_server.get_app()
app.run(host='0.0.0.0', port=8000)
```
Sample URLs:
    http://localhost:8000/tl/ta/amma
    http://localhost:8000/languages

FORMAT:
    Based on the Varnam API standard
    https://api.varnamproject.com/tl/hi/bharat
"""

from flask import Flask, jsonify, request, make_response
from flask_limiter import Limiter
from uuid import uuid4
from datetime import datetime
import traceback
import enum
import os
import redis
import json

from .utils import (
    LANG_CODE_TO_DISPLAY_NAME,
    RTL_LANG_CODES,
    LANG_CODE_TO_SCRIPT_CODE,
    GOOGLE_FONTS,
    FALLBACK_FONTS,
)


class XlitError(enum.Enum):
    lang_err = "Unsupported langauge ID requested ;( Please check available languages."
    string_err = "String passed is incompatable ;("
    internal_err = "Internal crash ;("
    unknown_err = "Unknown Failure"
    loading_err = "Loading failed ;( Check if metadata/paths are correctly configured."


def get_remote_address() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr) or "127.0.0.1"


app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri="memory://",
)

## ----------------------------- Xlit Engine -------------------------------- ##

from .xlit_src import XlitEngine

MAX_SUGGESTIONS = 8
DEFAULT_NUM_SUGGESTIONS = 5

ENGINE = {
    "en2indic": XlitEngine(
        beam_width=MAX_SUGGESTIONS,
        rescore=True,
        model_type="transformer",
        src_script_type="roman",
    ),
    "indic2en": XlitEngine(
        beam_width=MAX_SUGGESTIONS,
        rescore=False,
        model_type="transformer",
        src_script_type="indic",
    ),
}

EXPOSED_LANGS = [
    {
        "LangCode": lang_code,  # ISO-639 code
        "Identifier": lang_code,  # ISO-639 code
        "DisplayName": LANG_CODE_TO_DISPLAY_NAME[lang_code],
        "Author": "AI4Bharat",  # Name of developer / team
        "CompiledDate": "09-April-2022",  # date on which model was trained
        "IsStable": True,  # Set `False` if the model is experimental
        "Direction": "rtl" if lang_code in RTL_LANG_CODES else "ltr",
        "ScriptCode": LANG_CODE_TO_SCRIPT_CODE[lang_code],
        "GoogleFont": GOOGLE_FONTS[lang_code] if lang_code in GOOGLE_FONTS else None,
        "FallbackFont": (
            FALLBACK_FONTS[lang_code] if lang_code in FALLBACK_FONTS else None
        ),
    }
    for lang_code in sorted(ENGINE["en2indic"].all_supported_langs)
]


def get_app():
    return app, ENGINE


## ---------------------------- API End-points ------------------------------ ##


@app.route("/languages", methods=["GET", "POST"])
def supported_languages():
    # Format - https://xlit-api.ai4bharat.org/languages
    response = make_response(jsonify(EXPOSED_LANGS))
    if "xlit_user_id" not in request.cookies:
        # host = request.environ['HTTP_ORIGIN'].split('://')[1]
        host = ".ai4bharat.org"
        response.set_cookie(
            "xlit_user_id",
            uuid4().hex,
            max_age=365 * 24 * 60 * 60,
            domain=host,
            samesite="None",
            secure=True,
            httponly=True,
        )
    return response


@app.route("/tl/<lang_code>/<eng_word>", methods=["GET"])
@limiter.limit("5/second")
def xlit_api(lang_code, eng_word):
    # Format: https://xlit-api.ai4bharat.org/tl/ta/bharat
    response = {
        "success": False,
        "error": "",
        "at": str(datetime.utcnow()) + " +0000 UTC",
        "input": eng_word.strip(),
        "result": "",
    }

    transliterate_numerals = request.args.get(
        "transliterate_numerals", default=False, type=lambda v: v.lower() == "true"
    )
    num_suggestions = request.args.get(
        "num_suggestions", default=DEFAULT_NUM_SUGGESTIONS, type=int
    )

    if lang_code not in ENGINE["en2indic"].all_supported_langs:
        response["error"] = (
            "Invalid scheme identifier. Supported languages are: "
            + str(ENGINE["en2indic"].all_supported_langs)
        )
        return jsonify(response)

    try:
        ## Limit char count to --> 70
        xlit_result = ENGINE["en2indic"].translit_word(
            eng_word[:70],
            lang_code,
            topk=num_suggestions,
            transliterate_numerals=transliterate_numerals,
        )
    except Exception:
        xlit_result = XlitError.internal_err

    if isinstance(xlit_result, XlitError):
        response["error"] = xlit_result.value
        print("XlitError:", traceback.format_exc())
    else:
        response["result"] = xlit_result
        response["success"] = True

    return jsonify(response)


@app.route("/rtl/<lang_code>/<word>", methods=["GET"])
@limiter.limit("5/second")
def reverse_xlit_api(lang_code, word):
    # Format: https://api.varnamproject.com/rtl/hi/भारत
    response = {
        "success": False,
        "error": "",
        "at": str(datetime.utcnow()) + " +0000 UTC",
        "input": word.strip(),
        "result": "",
    }

    if lang_code not in ENGINE["indic2en"].all_supported_langs:
        response["error"] = (
            "Invalid scheme identifier. Supported languages are: "
            + str(ENGINE["indic2en"].all_supported_langs)
        )
        return jsonify(response)

    num_suggestions = request.args.get(
        "num_suggestions", default=DEFAULT_NUM_SUGGESTIONS, type=int
    )

    try:
        # Limit char count to --> 70
        xlit_result = ENGINE["indic2en"].translit_word(
            word[:70], lang_code, topk=num_suggestions
        )
    except Exception:
        xlit_result = XlitError.internal_err

    if isinstance(xlit_result, XlitError):
        response["error"] = xlit_result.value
        print("XlitError:", traceback.format_exc())
    else:
        response["result"] = xlit_result
        response["success"] = True

    return jsonify(response)


r = redis.Redis(
    host=os.getenv("REDIS_HOST"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    password=os.getenv("REDIS_PASSWORD", None),
    ssl=True,
)


@app.route("/transliterate", methods=["POST"])
def ulca_api():
    """
    ULCA-compliant endpoint. See for sample request-response:
    https://github.com/ULCA-IN/ulca/tree/master/specs/examples/model/transliteration-model
    """
    data = request.get_json(force=True)

    if "input" not in data or "config" not in data:
        return (
            jsonify(
                {
                    "status": {
                        "statusCode": 400,
                        "message": "Ensure `input` and `config` fields missing.",
                    }
                }
            ),
            400,
        )

    if (
        data["config"]["language"]["sourceLanguage"] == "en"
        and data["config"]["language"]["targetLanguage"]
        in ENGINE["en2indic"].all_supported_langs
    ) or (
        data["config"]["language"]["sourceLanguage"]
        in ENGINE["indic2en"].all_supported_langs
        and data["config"]["language"]["targetLanguage"] == "en"
    ):
        pass
    else:
        return (
            jsonify(
                {
                    "status": {
                        "statusCode": 501,
                        "message": "The mentioned language-pair is not supported yet.",
                    }
                }
            ),
            501,
        )

    is_sentence = (
        data["config"]["isSentence"] if "isSentence" in data["config"] else False
    )
    num_suggestions = (
        1
        if is_sentence
        else (
            data["config"]["numSuggestions"]
            if "numSuggestions" in data["config"]
            else 5
        )
    )

    if data["config"]["language"]["targetLanguage"] == "en":
        engine = ENGINE["indic2en"]
        lang_code = data["config"]["language"]["sourceLanguage"]
    else:
        engine = ENGINE["en2indic"]
        lang_code = data["config"]["language"]["targetLanguage"]

    for item in data["input"]:
        # Check if the transliteration is in the cache
        key = json.dumps(
            [
                item["source"],
                data["config"]["language"]["sourceLanguage"],
                data["config"]["language"]["targetLanguage"],
            ]
        )
        value = r.get(key)
        if value is not None:
            item["target"] = [value.decode("utf-8")]
        elif is_sentence:
            item["target"] = [
                engine.translit_sentence(item["source"], lang_code=lang_code)
            ]
            r.set(key, item["target"][0].encode("utf-8"))
        else:
            item["source"] = item["source"][:32]
            item["target"] = engine.translit_word(
                item["source"], lang_code=lang_code, topk=num_suggestions
            )
            r.set(key, item["target"][0].encode("utf-8"))

    return {
        "output": data["input"],
        # "status": {
        #     "statusCode": 200,
        #     "message" : "success"
        # }
    }, 200
