/*
 * The reading player: light the verse the narration has reached, and let a
 * click on a verse seek to it.
 *
 * Plain vanilla JS in one file, served from /static like the vendored htmx —
 * no CDN, no build step, no dependency. All the timing comes from the .vtt
 * `corpus/audio.py` wrote, whose every cue payload is a verse number, so there
 * is no arithmetic about durations here at all.
 *
 * htmx replaces the whole #reader element when the mode changes, so `attach`
 * runs again after every swap and is idempotent (`data-wired`).
 *
 * It also tells the server which verse the narration has reached, so closing the
 * tab mid-chapter and coming back resumes rather than restarts. One POST per
 * verse, not per timeupdate — a cue change is already the right granularity.
 */
(function () {
  "use strict";

  function offscreen(element) {
    var box = element.getBoundingClientRect();
    return box.top < 0 || box.bottom > window.innerHeight;
  }

  function cueStart(track, number) {
    var cues = track.cues || [];
    for (var i = 0; i < cues.length; i++) {
      if (cues[i].text.trim() === number) return cues[i].startTime;
    }
    return undefined;
  }

  function keepPlace(verse) {
    var at = document.getElementById("reader-place");
    if (!at) return;
    var body = new FormData();
    body.append("verse", verse);
    // `keepalive` so the last verse before the tab closes is still recorded.
    fetch(at.value, { method: "POST", body: body, keepalive: true }).catch(function () {});
  }

  function attach() {
    var audio = document.getElementById("reader-audio");
    var element = document.getElementById("reader-cues");
    if (!audio || !element || audio.dataset.wired) return;
    audio.dataset.wired = "yes";
    var track = element.track;
    track.mode = "hidden";

    track.addEventListener("cuechange", function () {
      var cue = track.activeCues && track.activeCues[0];
      if (!cue) return;
      var number = cue.text.trim();
      keepPlace(number);
      var current = null;
      document.querySelectorAll(".verse").forEach(function (verse) {
        var on = verse.dataset.verse === number;
        verse.classList.toggle("verse-current", on);
        if (on) current = verse;
      });
      // Only once it has actually left the viewport: a page that scrolls under
      // someone following along is worse than one that does not move.
      if (current && offscreen(current)) {
        current.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });

    document.querySelectorAll(".verse").forEach(function (verse) {
      verse.addEventListener("click", function () {
        var at = cueStart(track, verse.dataset.verse);
        if (at === undefined) return;
        audio.currentTime = at;
        audio.play();
      });
    });
  }

  document.addEventListener("DOMContentLoaded", attach);
  document.body.addEventListener("htmx:afterSwap", attach);
})();
