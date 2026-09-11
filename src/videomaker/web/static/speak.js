/*
 * Read the passage aloud with the browser's own voice.
 *
 * The fallback for a deployment with no server voice — the `ML=0` image has no
 * Kokoro, and a 512 MB box could not hold it anyway. `speechSynthesis` is on the
 * device already: no model, no file, no RAM on the server, no dependency here.
 *
 * What it is not: this produces no audio file, so nothing it says can be
 * downloaded, put in the podcast feed, or bundled in the zip. Those need a real
 * reading on disk. The page says so rather than letting anyone find out.
 *
 * **One utterance per verse, not one per chapter.** `onboundary` would give
 * word-level position, but Safari has never fired it reliably and a chapter-long
 * utterance cannot be stopped anywhere but the start. A verse is the granularity
 * the rest of the reader already works in — `.verse-current`, the place the
 * server remembers — so speaking verse by verse gets the highlighting and the
 * bookmark for nothing, and keeps the arithmetic at zero.
 *
 * It starts at the top of the chapter and there is no click-a-verse-to-start:
 * that cost six lines and put this file over the sixty the size test allows,
 * which is the test saying the design was growing. Add it by giving the panel a
 * verse to start from, not by raising the limit.
 */
(function () {
  "use strict";

  var speech = window.speechSynthesis;

  function light(verse) {
    document.querySelectorAll(".verse").forEach(function (each) {
      each.classList.toggle("verse-current", each === verse);
    });
    var at = document.getElementById("reader-place");
    if (!at || !verse) return;
    var body = new FormData();
    body.append("verse", verse.dataset.verse);
    fetch(at.value, { method: "POST", body: body, keepalive: true }).catch(function () {});
  }

  function stop(button) {
    speech.cancel();
    delete button.dataset.speaking;
    button.textContent = "Read aloud";
    light(null);
  }

  function start(button) {
    var list = document.querySelectorAll(".verse");
    list.forEach(function (verse, index) {
      var said = new SpeechSynthesisUtterance(verse.textContent.trim());
      // The document's own language, so a browser with several voices installed
      // picks the right one rather than reading French with an English mouth.
      said.lang = document.documentElement.lang || "en";
      said.onstart = function () {
        light(verse);
      };
      if (index === list.length - 1) said.onend = function () {
        stop(button);
      };
      speech.speak(said);
    });
    button.dataset.speaking = "yes";
    button.textContent = "Stop";
    // A browser can have the API and no voice at all — headless Chromium does,
    // and so do some stripped Linux and Android builds. `getVoices()` cannot be
    // trusted at click time because it fills in asynchronously, so the test is
    // whether anything is actually speaking a moment later.
    window.setTimeout(function () {
      if (speech.speaking || speech.pending) return;
      stop(button);
      var said = document.getElementById("speak-status");
      if (said) said.textContent = "This device has no speech voice installed.";
    }, 800);
  }

  function attach() {
    var panel = document.getElementById("speak");
    var button = document.getElementById("speak-start");
    // Shown only once the API is known to exist: a button that does nothing is
    // worse than no button, and this is the one browser feature here a device
    // may simply not have.
    if (!panel || !button || button.dataset.wired || !speech) return;
    button.dataset.wired = "yes";
    panel.hidden = false;

    button.addEventListener("click", function () {
      if (button.dataset.speaking) stop(button);
      else start(button);
    });
    // Chrome keeps speaking after the page is gone unless it is told otherwise.
    window.addEventListener("pagehide", function () {
      speech.cancel();
    });
  }

  document.addEventListener("DOMContentLoaded", attach);
  document.body.addEventListener("htmx:afterSwap", attach);
})();
