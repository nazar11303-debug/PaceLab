// RunTracker — drag & drop для завантаження файлів.

(function () {
  const zone = document.getElementById("dropzone");
  const input = document.getElementById("file-input");
  const nameEl = document.getElementById("file-name");

  if (!zone || !input || !nameEl) return;

  const ALLOWED = [".gpx", ".fit"];

  function showFileName() {
    if (input.files && input.files.length > 0) {
      const file = input.files[0];
      const ext = "." + file.name.split(".").pop().toLowerCase();
      nameEl.textContent = (ALLOWED.includes(ext) ? "✓ " : "⚠ ") + file.name;
      nameEl.classList.remove("hidden");
    } else {
      nameEl.classList.add("hidden");
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
      // старі браузери: DataTransfer недоступний, файл доведеться вибрати вручну
      console.warn("Не вдалося підставити файл у форму:", error);
    }
    showFileName();
  }

  input.addEventListener("change", showFileName);

  ["dragenter", "dragover"].forEach(function (eventName) {
    zone.addEventListener(eventName, function (event) {
      event.preventDefault();
      event.stopPropagation();
      zone.classList.add("dropzone-active");
    });
  });

  ["dragleave", "dragend"].forEach(function (eventName) {
    zone.addEventListener(eventName, function (event) {
      event.preventDefault();
      event.stopPropagation();
      zone.classList.remove("dropzone-active");
    });
  });

  zone.addEventListener("drop", function (event) {
    event.preventDefault();
    event.stopPropagation();
    zone.classList.remove("dropzone-active");
    const files = event.dataTransfer && event.dataTransfer.files;
    acceptFiles(files);
  });

  // Не даємо браузеру відкрити файл, якщо його кинули повз зону
  window.addEventListener("dragover", function (event) {
    event.preventDefault();
  });
  window.addEventListener("drop", function (event) {
    event.preventDefault();
  });
})();
