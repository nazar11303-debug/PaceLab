// PaceLab — drag & drop, PWA-реєстрація.

(function () {
  // ── drag & drop для завантаження файлів ──────────────────────────────
  const zone = document.getElementById("dropzone");
  const input = document.getElementById("file-input");
  const nameEl = document.getElementById("file-name");

  if (zone && input && nameEl) {
    const ALLOWED = [".gpx", ".fit"];

    function showFileName() {
      if (input.files && input.files.length > 0) {
        const file = input.files[0];
        const extension = "." + file.name.split(".").pop().toLowerCase();
        nameEl.textContent = (ALLOWED.includes(extension) ? "✓ " : "⚠ ") + file.name;
        nameEl.hidden = false;
      } else {
        nameEl.hidden = true;
        nameEl.textContent = "";
      }
    }

    function acceptFiles(files) {
      if (!files || files.length === 0) return;
      try {
        const transfer = new DataTransfer();
        transfer.items.add(files[0]);
        input.files = transfer.files;
      } catch (error) {
        console.warn("Не вдалося підставити файл у форму:", error);
      }
      showFileName();
    }

    input.addEventListener("change", showFileName);

    ["dragenter", "dragover"].forEach((eventName) => {
      zone.addEventListener(eventName, (event) => {
        event.preventDefault();
        event.stopPropagation();
        zone.classList.add("dropzone-active");
      });
    });

    ["dragleave", "dragend"].forEach((eventName) => {
      zone.addEventListener(eventName, (event) => {
        event.preventDefault();
        event.stopPropagation();
        zone.classList.remove("dropzone-active");
      });
    });

    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      event.stopPropagation();
      zone.classList.remove("dropzone-active");
      acceptFiles(event.dataTransfer && event.dataTransfer.files);
    });

    window.addEventListener("dragover", (event) => event.preventDefault());
    window.addEventListener("drop", (event) => event.preventDefault());
  }

  // ── PWA: реєстрація service worker (працює на localhost/HTTPS) ──────
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {
        /* на LAN-IP без HTTPS реєстрація недоступна — це нормально */
      });
    });
  }
})();

