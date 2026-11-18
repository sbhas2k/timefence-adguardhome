/* The kid screen: one calm page, per group a big time-left line (no inline handlers). */
(function () {
  "use strict";
  var body = document.body;
  var base = body.dataset.base || "";
  var csrf = body.dataset.csrf || "";
  var i18n = {};
  var timezone = "UTC";
  var sequence = 0;
  var applied = 0;
  var timer = null;

  var REFRESH_MS = 30000;

  function $(sel) { return document.querySelector(sel); }
  function show(el, message) { if (el) { el.textContent = message; el.classList.remove("hidden"); } }
  function hide(el) { if (el) { el.classList.add("hidden"); } }

  /* A function replacer keeps "$&" and friends in names literal. */
  function t(key, vars) {
    var template = i18n[key] || key;
    return String(template).replace(/\{(\w+)\}/g, function (whole, name) {
      if (vars && Object.prototype.hasOwnProperty.call(vars, name)) { return String(vars[name]); }
      return whole;
    });
  }

  function clock(ts) {
    var seconds = Number(ts);
    if (!seconds) { return ""; }
    try {
      return new Intl.DateTimeFormat(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        timeZone: timezone
      }).format(new Date(seconds * 1000));
    } catch (err) {
      return "";
    }
  }

  function history(list) {
    return Array.isArray(list) ? list : [];
  }

  function progressFor(group) {
    var total = Math.max(0, (Number(group.minutes_today) || 0) + (Number(group.bonus_minutes) || 0));
    var remaining = Math.max(0, Number(group.remaining_minutes) || 0);
    var progress = document.createElement("progress");
    progress.className = "kid-bar";
    progress.max = total > 0 ? total : 1;
    progress.value = Math.min(remaining, progress.max);
    return progress;
  }

  function bigLine(group) {
    if (group.status === "bedtime") {
      var until = group.bedtime_until;
      return {
        main: until ? t("kid.bedtime", { time: until }) : t("kid.bedtime_any"),
        detail: ""
      };
    }
    var remaining = Math.max(0, Number(group.remaining_minutes) || 0);
    if (group.status === "limit_reached") {
      return { main: t("kid.time_up"), detail: t("kid.back_tomorrow") };
    }
    if (group.status === "blocked_admin") {
      return { main: t("kid.blocked_by_parent", { time: clock(group.blocked_until) }), detail: "" };
    }
    if (group.status === "paused") {
      return { main: t("kid.paused"), detail: "" };
    }
    if (group.status === "free") {
      return { main: t("kid.free_until", { time: clock(group.free_until) }), detail: "" };
    }
    if (remaining > 0 && remaining < 1) {
      return { main: t("kid.less_than_minute"), detail: "" };
    }
    return { main: t("kid.minutes_left", { minutes: Math.floor(remaining) }), detail: "" };
  }

  function card(group) {
    var article = document.createElement("article");
    article.className = "kid-card" + (group.status === "bedtime" ? " bedtime" : "");

    var emoji = document.createElement("span");
    emoji.className = "kid-emoji";
    emoji.setAttribute("aria-hidden", "true");
    emoji.textContent = group.emoji || "";

    var name = document.createElement("h2");
    name.className = "kid-name";
    name.textContent = group.name || "";

    var line = bigLine(group);
    var main = document.createElement("p");
    main.className = "kid-big";
    main.textContent = line.main;

    var bar = progressFor(group);
    bar.setAttribute("aria-label", line.main);

    var used = document.createElement("p");
    used.className = "kid-used";
    used.textContent = t("kid.used_today", {
      minutes: Math.round(Number(group.used_minutes) || 0)
    });

    article.appendChild(emoji);
    article.appendChild(name);
    article.appendChild(main);
    article.appendChild(bar);
    article.appendChild(used);
    if (line.detail) {
      var detail = document.createElement("p");
      detail.className = "kid-detail";
      detail.textContent = line.detail;
      article.appendChild(detail);
    }
    return article;
  }

  function render(state) {
    var list = $("#kid-groups");
    if (!list) { return; }
    while (list.firstChild) { list.removeChild(list.firstChild); }
    var groups = history(state.groups);
    for (var index = 0; index < groups.length; index += 1) {
      list.appendChild(card(groups[index]));
    }
    list.setAttribute("aria-busy", "false");
    if (groups.length) { hide($("#kid-empty")); } else { show($("#kid-empty"), t("kid.empty")); }

    var user = $("#kid-user");
    if (user && state.app && state.app.username) { user.textContent = state.app.username; }

    var updated = $("#kid-updated");
    if (updated && state.app) {
      updated.textContent = t("kid.updated", { time: clock(state.app.now) });
    }
  }

  function refresh() {
    var mine = sequence + 1;
    sequence = mine;
    return fetch(base + "/api/state", { credentials: "same-origin" })
      .then(function (response) {
        if (response.status === 401) {
          window.location.href = base + "/login";
          throw new Error("login required");
        }
        if (!response.ok) { throw new Error("HTTP " + response.status); }
        return response.json();
      })
      .then(function (state) {
        if (mine < applied) { return; }  /* a late, older response is dropped */
        applied = mine;
        if (state.app && state.app.timezone) { timezone = state.app.timezone; }
        render(state);
        hide($("#kid-offline"));
      })
      .catch(function (error) {
        if (mine < applied || String(error && error.message) === "login required") { return; }
        show($("#kid-offline"), t("kid.offline"));
      });
  }

  function schedule() {
    if (timer) { clearInterval(timer); timer = null; }
    if (typeof document.hidden !== "undefined" && document.hidden) { return; }
    timer = setInterval(refresh, REFRESH_MS);
  }

  function readI18n() {
    return fetch(base + "/api/i18n", { credentials: "same-origin" })
      .then(function (response) { return response.json(); })
      .then(function (strings) { i18n = strings || {}; })
      .catch(function () {});
  }

  function logout() {
    return fetch(base + "/api/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: "{}"
    }).then(function () {
      window.location.href = base + "/login";
    }).catch(function () {
      window.location.href = base + "/login";
    });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (timer) { clearInterval(timer); timer = null; }
      return;
    }
    refresh();
    schedule();
  });
  window.addEventListener("focus", function () { refresh(); });

  var button = $("#kid-logout");
  if (button) { button.addEventListener("click", logout); }

  readI18n().then(function () {
    refresh();
    schedule();
  });
}());
