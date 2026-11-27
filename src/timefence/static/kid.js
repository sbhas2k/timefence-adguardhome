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

  /* 24-hour HH:MM in the server zone, never the browser's 12-hour form. */
  function clock(ts) {
    var seconds = Number(ts);
    if (!seconds) { return ""; }
    try {
      return new Intl.DateTimeFormat("en-GB", {
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
        timeZone: timezone
      }).format(new Date(seconds * 1000));
    } catch (err) {
      return "";
    }
  }

  function asList(list) {
    return Array.isArray(list) ? list : [];
  }

  /* The server names the local day of a future time ("tomorrow", "sat"): the
     kid's device clock and zone never get a say. */
  function dayWord(day) {
    var keys = {
      mon: "card.day_mon",
      tue: "card.day_tue",
      wed: "card.day_wed",
      thu: "card.day_thu",
      fri: "card.day_fri",
      sat: "card.day_sat",
      sun: "card.day_sun"
    };
    if (day === "tomorrow") { return t("common.tomorrow"); }
    return keys[day] ? t(keys[day]) : "";
  }

  /* One sentence per group, straight from the reduced payload: the server
     already decided whether the group is open, so no clock maths here. */
  function bigLine(group) {
    if (group.status === "free") {
      return group.free_until_day
        ? t("kid.free_until_day", {
          day: dayWord(group.free_until_day),
          time: clock(group.free_until)
        })
        : t("kid.free_until", { time: clock(group.free_until) });
    }
    if (group.status === "blocked_admin") { return t("kid.blocked"); }
    if (group.status === "paused") { return t("kid.paused"); }
    if (group.status === "outside_hours") {
      return group.opens_day
        ? t("kid.opens_day", { day: dayWord(group.opens_day), time: group.opens_at || "" })
        : t("kid.opens_at", { time: group.opens_at || "" });
    }
    if (group.open_until) {
      return group.open_until_day
        ? t("kid.open_until_day", {
          day: dayWord(group.open_until_day),
          time: group.open_until
        })
        : t("kid.open_until", { time: group.open_until });
    }
    return t("kid.open_all_day");
  }

  function card(group) {
    var article = document.createElement("article");
    /* Closed outside the allowed times is calm, not a punishment. */
    article.className = "kid-card" + (group.status === "outside_hours" ? " closed" : "");

    var emoji = document.createElement("span");
    emoji.className = "kid-emoji";
    emoji.setAttribute("aria-hidden", "true");
    emoji.textContent = group.emoji || "";

    var name = document.createElement("h2");
    name.className = "kid-name";
    name.textContent = group.name || "";

    var main = document.createElement("p");
    main.className = "kid-big";
    main.textContent = bigLine(group);

    article.appendChild(emoji);
    article.appendChild(name);
    article.appendChild(main);
    return article;
  }

  function render(state) {
    var list = $("#kid-groups");
    if (!list) { return; }
    while (list.firstChild) { list.removeChild(list.firstChild); }
    var groups = asList(state.groups);
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
