/* Raceline Studio — boot sequence.
   /init -> store -> map image -> view -> tools -> ui -> autosave. */

import { api } from "./api.js";
import { initStore, startAutosave } from "./store.js";
import { initTools } from "./tools.js";
import { initUI } from "./ui.js";
import { initView } from "./view.js";

const bootMsg = (m) => {
  const e = document.getElementById("bootMsg");
  if (e) e.textContent = m;
};
const bootFail = (m) => {
  bootMsg(m);
  const bar = document.getElementById("bootBar");
  if (bar) bar.style.background = "#ff4d4d";
};

async function boot() {
  try {
    if (window.gsap) {
      gsap.to("#bootBar", { width: "55%", duration: 0.9, ease: "power1.out" });
    }
    bootMsg("loading map + lines…");
    const data = await api.init();

    bootMsg("decoding map…");
    const img = await new Promise((resolve, reject) => {
      const im = new Image();
      im.onload = () => resolve(im);
      im.onerror = () => reject(new Error("map image decode failed"));
      im.src = "data:image/png;base64," + data.map_b64;
    });

    initStore(data);
    initView(img);
    initTools();
    initUI(data);
    startAutosave();

    // debugging hook (console / e2e tests): inspect state + transforms
    const view = await import("./view.js");
    window.__RL = { S: (await import("./store.js")).S,
                    worldToCanvas: view.worldToCanvas };
  } catch (e) {
    console.error(e);
    bootFail(`boot failed: ${e.message} — is raceline_studio.py running?`);
  }
}

boot();
