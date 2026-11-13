/* Login and first-run setup. No inline handlers, no globals. */
(function () {
  "use strict";
  var body = document.body;
  var base = body.dataset.base || "";
  var page = body.dataset.page;
  var csrf = body.dataset.csrf || "";
  var i18n = {};

  function $(sel) { return document.querySelector(sel); }
  function show(el, message) {
    if (!el) { return; }
    el.textContent = message;
    el.classList.remove("hidden");
  }
  function hide(el) { if (el) { el.classList.add("hidden"); } }

  function api(path, payload) {
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
    .catch(function () { i18n = {}; });

  fetch(base + "/api/meta", { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (meta) {
      var hint = $("#setup-hint");
      if (!meta.setup_complete && hint) { hint.classList.remove("hidden"); }
    })
    .catch(function () {});

  if (page === "login") {
    var pin = "";
    var display = $("#pin-display");
    var error = $("#login-error");
    var role = "user";

    var tabs = { user: $("#tab-user"), admin: $("#tab-admin") };
    var panes = { user: $("#pane-user"), admin: $("#pane-admin") };

    function selectRole(next) {
      role = next;
      Object.keys(tabs).forEach(function (key) {
        tabs[key].setAttribute("aria-selected", key === next ? "true" : "false");
        panes[key].classList.toggle("hidden", key !== next);
      });
      hide(error);
      if (next === "admin") {
        var pw = $("#admin-password");
        if (pw) { pw.focus(); }
      }
    }
    if (tabs.user) { tabs.user.addEventListener("click", function () { selectRole("user"); }); }
    if (tabs.admin) { tabs.admin.addEventListener("click", function () { selectRole("admin"); }); }

    function renderPin() {
      if (!display) { return; }
      display.textContent = pin.replace(/./g, "•");
    }

    function submitPin() {
      if (pin.length < 4) { return; }
      login("user", pin);
    }

    function login(asRole, secret) {
      hide(error);
      api("/api/login", { role: asRole, secret: secret }).then(function (data) {
        if (!data.setup_complete && data.role === "admin") {
          window.location.href = base + "/setup";
          return;
        }
        window.location.href = base + "/";
      }).catch(function (err) {
        show(error, err.message || (i18n["login.wrong"] || "Wrong PIN or password"));
        pin = "";
        renderPin();
        var pw = $("#admin-password");
        if (pw) { pw.value = ""; }
      });
    }

    var keypad = $("#keypad");
    if (keypad) {
      keypad.addEventListener("click", function (event) {
        var button = event.target.closest("[data-key]");
        if (!button) { return; }
        var key = button.dataset.key;
        if (key === "back") { pin = pin.slice(0, -1); }
        else if (key === "clear") { pin = ""; }
        else if (/^\d$/.test(key) && pin.length < 8) { pin += key; }
        renderPin();
        hide(error);
      });
    }
    var submit = $("#pin-submit");
    if (submit) { submit.addEventListener("click", submitPin); }

    document.addEventListener("keydown", function (event) {
      if (role !== "user") { return; }
      if (/^\d$/.test(event.key) && pin.length < 8) { pin += event.key; renderPin(); }
      else if (event.key === "Backspace") { pin = pin.slice(0, -1); renderPin(); }
      else if (event.key === "Enter") { submitPin(); }
    });

    var adminForm = $("#admin-form");
    if (adminForm) {
      adminForm.addEventListener("submit", function (event) {
        event.preventDefault();
        login("admin", ($("#admin-password") || {}).value || "");
      });
    }
    renderPin();
  }

  if (page === "setup") {
    var form = $("#setup-form");
    var setupError = $("#setup-error");
    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        hide(setupError);
        var adminPassword = ($("#admin_password") || {}).value || "";
        var adminPassword2 = ($("#admin_password2") || {}).value || "";
        var pin = ($("#user_pin") || {}).value || "";
        var pin2 = ($("#user_pin2") || {}).value || "";
        if (adminPassword !== adminPassword2 || pin !== pin2) {
          show(setupError, i18n["setup.mismatch"] || "The values do not match");
          return;
        }
        api("/api/setup", { admin_password: adminPassword, user_pin: pin })
          .then(function () { window.location.href = base + "/"; })
          .catch(function (err) { show(setupError, err.message); });
      });
    }
  }
})();
