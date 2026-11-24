// Maps a search position to the colour class used by the matrix, badges and chart
// (mirrors the Jinja filter `position_class` in dashboard/__init__.py).
function positionClass(rank) {
  const value = Number(rank);
  if (rank === null || rank === undefined || !Number.isFinite(value) || value <= 0) return "outside";
  if (value <= 3) return "top-3";
  if (value <= 6) return "top-6";
  if (value <= 10) return "top-10";
  if (value <= 20) return "top-20";
  return "outside";
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { positionClass };
} else {
  window.positionClass = positionClass;
}
