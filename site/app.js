"use strict";
const repository = "tm-henningnt/Igloo";
const releases = "https://github.com/" + repository + "/releases";
function releaseDownloads(release) {
  if (!release || release.draft || release.prerelease ||
      !/^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))?$/.test(release.tag_name) ||
      release.html_url !== releases + "/tag/" + release.tag_name || !Array.isArray(release.assets)) return null;
  const version = release.tag_name.slice(1);
  const downloads = {};
  for (const runtime of ["bundled", "system"]) {
    const filename = "Igloo-" + version + "-universal" + (runtime === "system" ? "-system-python" : "") + ".dmg";
    const matches = release.assets.filter(asset => asset.name === filename);
    if (matches.length === 1 && matches[0].state === "uploaded" && Number.isSafeInteger(matches[0].size) && matches[0].size > 0 &&
        matches[0].browser_download_url === releases + "/download/" + release.tag_name + "/" + filename) {
      downloads[runtime] = matches[0].browser_download_url;
    }
  }
  return downloads.bundled ? { version, ...downloads } : null;
}
async function showDownloads() {
  try {
    const response = await fetch("https://api.github.com/repos/" + repository + "/releases/latest", {
      headers: { Accept: "application/vnd.github+json" }, credentials: "omit", signal: AbortSignal.timeout(8000)
    });
    if (!response.ok) return;
    const data = releaseDownloads(await response.json());
    if (!data) return;
    const standard = document.getElementById("download-standard");
    standard.href = data.bundled;
    standard.textContent = "Download Igloo " + data.version + " for Mac ↓";
    document.getElementById("release-status").textContent = "Igloo " + data.version + " · Apple silicon and Intel Macs";
    if (data.system) {
      const system = document.getElementById("download-system");
      system.href = data.system;
      system.textContent = "Download " + data.version + " · system Python →";
    }
  } catch {
    // Static release links remain usable when the API is unavailable.
  }
}
function setupAppearanceComparison() {
  const comparison = document.querySelector("[data-appearance-comparison]");
  const slider = document.getElementById("appearance-slider");
  if (!comparison || !slider) return;
  const update = () => {
    const light = Math.max(0, Math.min(100, Number(slider.value)));
    comparison.style.setProperty("--split", light + "%");
    slider.setAttribute("aria-valuetext", light + "% light mode, " + (100 - light) + "% dark mode");
  };
  slider.disabled = false;
  slider.addEventListener("input", update);
  update();
}
if (typeof document !== "undefined") { setupAppearanceComparison(); showDownloads(); }
if (typeof module !== "undefined") module.exports = { releaseDownloads };
