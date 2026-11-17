/* Login and first-run setup (no inline handlers). */
(function () {
  "use strict";
  var body = document.body;
  var base = body.dataset.base || "";
  var page = body.dataset.page;
  var csrf = body.dataset.csrf || "";
  var i18n = {};

  function $(sel) { return document.querySelector(sel); }
  function show(el, message) { if (el) { el.textContent = message; el.classList.remove("hidden"); } }
  function hide(el) { if (el) { el.classList.add("hidden"); } }

  function post(path, payload) {
    return fetch(base + path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(payload || {})
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) { throw new Error(data.error || ("HTTP " + response.status)); }
        return data;
      });
    });
  }

  fetch(base + "/api/i18n", { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (d) { i18n = d || {}; })
    .catch(function () {});

  fetch(base + "/api/meta", { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (meta) {
      var hint = $("#setup-hint");
      if (!meta.setup_complete && hint) { hint.classList.remove("hidden"); }
    })
    .catch(function () {});

  if (page === "login") {
    var form = $("#login-form");
    var error = $("#login-error");
    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        hide(error);
        post("/api/login", {
          username: ($("#username").value || "").trim(),
          password: $("#password").value || ""
        }).then(function (data) {
          if (!data.setup_complete && data.role === "admin") {
            window.location.href = base + "/setup";
            return;
          }
          window.location.href = base + "/";
        }).catch(function (err) {
          show(error, err.message || (i18n["login.wrong"] || "Wrong username or password"));
          var password = $("#password");
          if (password) { password.value = ""; password.focus(); }
        });
      });
    }
    var user = $("#username");
    if (user) { user.focus(); }
  }

  if (page === "setup") {
    var setupForm = $("#setup-form");
    var setupError = $("#setup-error");
    if (setupForm) {
      setupForm.addEventListener("submit", function (event) {
        event.preventDefault();
        hide(setupError);
        var first = ($("#admin_password").value || "");
        var second = ($("#admin_password2").value || "");
        if (first !== second) {
          show(setupError, i18n["setup.mismatch"] || "The values do not match");
          return;
        }
        post("/api/setup", { admin_password: first })
          .then(function () { window.location.href = base + "/"; })
          .catch(function (err) { show(setupError, err.message); });
      });
    }
  }
})();
