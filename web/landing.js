// appliedin.dev — the two behaviours a documentation page is expected to have.
//
// Neither is decoration. A sidebar that does not track the reader stops being a
// map and becomes a list, and a heading you cannot link to is a section nobody
// can point a colleague at.

(function () {
  "use strict";

  var headings = Array.prototype.slice.call(
    document.querySelectorAll("article h2[id], article h3[id]")
  );
  var links = Array.prototype.slice.call(document.querySelectorAll(".sidebar a[href^='#']"));
  if (!headings.length || !links.length) return;

  // --- a link on every heading ---------------------------------------------
  headings.forEach(function (h) {
    var a = document.createElement("a");
    a.className = "anchor";
    a.href = "#" + h.id;
    a.setAttribute("aria-label", "Link to this section");
    a.textContent = "#";
    h.appendChild(a);
  });

  // --- highlight the section being read ------------------------------------
  var byHash = {};
  links.forEach(function (a) { byHash[a.getAttribute("href").slice(1)] = a; });

  var current = null;
  function mark(id) {
    if (id === current) return;
    current = id;
    links.forEach(function (a) { a.classList.remove("active"); });
    if (byHash[id]) byHash[id].classList.add("active");
  }

  // The topmost heading that has scrolled past the header is the one being read.
  // rootMargin does the work: a heading only counts once it is near the top, so
  // the highlight moves when the reader arrives rather than when a heading first
  // peeks into view at the bottom.
  if ("IntersectionObserver" in window) {
    var seen = new Set();
    var io = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting) seen.add(e.target.id);
          else seen.delete(e.target.id);
        });
        var first = headings.find(function (h) { return seen.has(h.id); });
        if (first) mark(first.id);
      },
      { rootMargin: "-72px 0px -70% 0px", threshold: 0 }
    );
    headings.forEach(function (h) { io.observe(h); });
  }
})();
