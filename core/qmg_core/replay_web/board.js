"use strict";

const world = {
  canada: [0, 1], "united-states": [0, 3], "latin-america": [1, 5],
  "north-atlantic": [1, 1], "central-atlantic": [2, 3], "south-atlantic": [3, 6],
  "united-kingdom": [2, 1], "western-europe": [3, 3], "north-sea": [3, 1],
  scandinavia: [4, 0], germany: [4, 2], italy: [4, 4], "baltic-sea": [5, 1],
  "eastern-europe": [5, 2], balkans: [5, 4], ukraine: [6, 3], moscow: [6, 1],
  russia: [7, 0], "black-sea": [6, 4], "north-africa": [4, 6], mediterranean: [4, 5],
  "south-africa": [4, 8], madagascar: [5, 8], "middle-east": [6, 5], "caspian-sea": [7, 3],
  "arabian-sea": [6, 7], "indian-ocean": [7, 8], kazakhstan: [8, 2], india: [8, 5],
  siberia: [9, 0], mongolia: [9, 2], "china-west": [9, 4], "china-east": [10, 3],
  vladivostok: [11, 1], japan: [12, 2], "east-china-sea": [11, 3], "iwo-jima": [12, 3],
  "southeast-asia": [9, 5], "south-china-sea": [10, 5], philippines: [11, 5],
  indonesia: [9, 6], "new-guinea": [11, 6], australia: [10, 8], "new-zealand": [12, 8],
  "north-pacific": [13, 1], hawaii: [13, 3], "central-pacific": [12, 5],
  "east-pacific": [14, 3], "southeast-pacific": [14, 6], "south-pacific": [13, 7],
};
// Percentage anchors from the frozen project's src/engine/map.ts.
const boardAnchors = {
  canada: [11, 18], "united-states": [11, 28], "latin-america": [19, 62],
  "united-kingdom": [30, 25], scandinavia: [36, 13], "western-europe": [31, 33],
  germany: [37, 29], italy: [36, 38], "eastern-europe": [39, 25], balkans: [40, 36],
  ukraine: [45, 29], moscow: [52, 20], russia: [47, 14], siberia: [63, 12],
  vladivostok: [79, 11], kazakhstan: [56, 31], mongolia: [65, 29],
  "china-west": [62, 40], "china-east": [71, 40], japan: [80, 32], "iwo-jima": [81, 46],
  "southeast-asia": [68, 53], philippines: [74, 56], indonesia: [71, 66],
  "new-guinea": [79, 66], india: [60, 49], "middle-east": [48, 45],
  "north-africa": [39, 52], "south-africa": [39, 62], madagascar: [46, 76],
  australia: [76, 78], "new-zealand": [84, 89], hawaii: [89, 43],
  "north-atlantic": [17, 40], "southeast-pacific": [10, 67], "central-atlantic": [29, 72],
  "south-atlantic": [30, 89], "north-sea": [26, 30], "baltic-sea": [38, 18],
  "black-sea": [44, 38], "caspian-sea": [51, 37], mediterranean: [42, 45],
  "arabian-sea": [54, 64], "indian-ocean": [59, 76], "south-china-sea": [72, 58],
  "east-china-sea": [79, 44], "north-pacific": [86, 27], "central-pacific": [85, 56],
  "east-pacific": [91, 59], "south-pacific": [84, 71],
};
const forceColors = {
  "soviet-union": "#c52f3d", china: "#934637", france: "#3265b8", "united-kingdom": "#777242",
  "united-states": "#23824b", italy: "#7f49a5", germany: "#222226", japan: "#f3e6c8",
};

function pieceIcon(kind, color, label, count = 1) {
  const piece = node("span", undefined, "piece");
  piece.title = label;
  piece.setAttribute("aria-label", label);
  piece.setAttribute("role", "img");
  const mold = node("span", undefined, "piece-mold");
  const shape = node("span", undefined, "piece-shape");
  shape.dataset.kind = kind.toLowerCase();
  shape.style.setProperty("--piece-color", color);
  mold.append(shape);
  piece.append(mold);
  if (count > 1) piece.append(node("small", "×" + count, "piece-count"));
  return piece;
}
