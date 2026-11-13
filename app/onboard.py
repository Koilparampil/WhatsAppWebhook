"""One-time page that runs Meta's Embedded Signup in WhatsApp Business app (Coexistence) mode.

A number that lives in the WhatsApp Business app can only be connected to Cloud API through
Embedded Signup with ``featureType: whatsapp_business_app_onboarding``; WhatsApp Manager and the
``/register`` endpoint cannot do it. The page only launches Meta's popup and shows the ids Meta
reports back. It stores nothing, and the authorization code is never displayed or sent anywhere:
the service keeps using its own System User token.
"""

from __future__ import annotations

import json

ONBOARD_HEADERS = {
    "Cache-Control": "no-store",
    # the URL carries ?key=; never leak it to connect.facebook.net or anywhere else
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}


def _js(value: str) -> str:
    """A JavaScript string literal that is also safe inside an HTML <script> element."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_onboard_page(app_id: str, config_id: str, api_version: str) -> str:
    return (
        _TEMPLATE.replace("__APP_ID__", _js(app_id))
        .replace("__CONFIG_ID__", _js(config_id))
        .replace("__API_VERSION__", _js(api_version))
    )


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex, nofollow">
<title>Connect the WhatsApp Business app number</title>
<style>
  body { font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 44rem;
         margin: 2rem auto; padding: 0 1rem; color: #1c1e21; }
  h1 { font-size: 1.4rem; }
  ol { padding-left: 1.2rem; }
  button { font: inherit; font-weight: 600; padding: .7rem 1.2rem; border: 0; border-radius: 6px;
           background: #1877f2; color: #fff; cursor: pointer; }
  button:disabled { background: #9cb4d8; cursor: default; }
  pre { background: #f0f2f5; padding: 1rem; border-radius: 6px; white-space: pre-wrap;
        word-break: break-word; min-height: 3rem; }
  .ok { color: #1e7e34; font-weight: 600; }
  .bad { color: #c62828; font-weight: 600; }
</style>
</head>
<body>
<h1>Connect the WhatsApp Business app number to the email forwarder</h1>
<ol>
  <li>Keep the phone with the <strong>WhatsApp Business app</strong> nearby, updated and online.</li>
  <li>Click the button and log in with a Facebook account that is an admin of the business portfolio.</li>
  <li>Choose the business portfolio, enter the business phone number, then confirm in the WhatsApp
      Business app when it asks (message or QR code). Sharing chat history is not needed.</li>
  <li>Leave this tab open until the result below says it finished.</li>
</ol>
<p><button id="start" disabled>Loading Facebook&hellip;</button></p>
<p id="status"></p>
<pre id="out"></pre>
<p>Afterwards, on the VM: <code>whatsapp-webhook meta-check</code>, update <code>WA_WABA_ID</code> /
<code>WA_PHONE_NUMBER_ID</code> if they changed, run <code>whatsapp-webhook meta-subscribe</code>,
then clear <code>ONBOARD_KEY</code> and restart the service.</p>
<script>
(function () {
  "use strict";
  var APP_ID = __APP_ID__;
  var CONFIG_ID = __CONFIG_ID__;
  var API_VERSION = __API_VERSION__;
  var btn = document.getElementById("start");
  var out = document.getElementById("out");
  var status = document.getElementById("status");

  function show(label, obj) {
    out.textContent += (out.textContent ? "\\n\\n" : "") + label + ":\\n" + JSON.stringify(obj, null, 2);
  }
  function setStatus(text, cls) { status.textContent = text; status.className = cls || ""; }

  window.addEventListener("message", function (event) {
    var origin;
    try { origin = new URL(event.origin); } catch (e) { return; }
    var host = origin.hostname;
    if (origin.protocol !== "https:") return;
    if (host !== "facebook.com" && host.slice(-13) !== ".facebook.com") return;
    var data = event.data;
    if (typeof data === "string") { try { data = JSON.parse(data); } catch (e) { return; } }
    if (!data || data.type !== "WA_EMBEDDED_SIGNUP") return;
    show("Meta reported", data);
    if (data.event === "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING" || data.event === "FINISH") {
      setStatus("Finished. Note the waba_id above and continue on the VM.", "ok");
    } else if (data.event === "CANCEL") {
      var step = (data.data && data.data.current_step) || "unknown";
      setStatus("Cancelled or failed at step: " + step, "bad");
    } else if (data.event === "ERROR") {
      setStatus("Meta reported an error (details above).", "bad");
    }
  });

  window.fbAsyncInit = function () {
    FB.init({ appId: APP_ID, autoLogAppEvents: true, xfbml: false, version: API_VERSION });
    btn.disabled = false;
    btn.textContent = "Connect WhatsApp Business app";
  };

  btn.addEventListener("click", function () {
    setStatus("Waiting for the Facebook popup\\u2026");
    FB.login(function (response) {
      if (response && response.authResponse && response.authResponse.code) {
        show("Login", { authorization_code_received: true });
        if (!status.className) setStatus("Login completed; waiting for Meta's onboarding result\\u2026");
      } else {
        show("Login", { authorization_code_received: false, status: response && response.status });
        if (!status.className) setStatus("The popup was closed before it finished.", "bad");
      }
    }, {
      config_id: CONFIG_ID,
      response_type: "code",
      override_default_response_type: true,
      extras: { setup: {}, featureType: "whatsapp_business_app_onboarding", sessionInfoVersion: "3" }
    });
  });

  setTimeout(function () {
    if (btn.disabled) {
      setStatus("Facebook's script did not load. Disable ad or tracker blockers here and reload.", "bad");
    }
  }, 10000);
})();
</script>
<script async defer crossorigin="anonymous" src="https://connect.facebook.net/en_US/sdk.js"></script>
</body>
</html>
"""
