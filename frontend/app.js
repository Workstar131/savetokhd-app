// =====================================================================
// CONFIGURATION
// =====================================================================
// Replace with your actual Render backend URL (without trailing slash)
const API_BASE_URL = "https://savetokhd.onrender.com"; 

// Keeps track of the current video link entered by the user
let currentOriginalUrl = "";

// =====================================================================
// DOM ELEMENTS
// =====================================================================
const singleForm = document.getElementById("single-form");
const urlInput = document.getElementById("url-input");
const singleBtn = document.getElementById("single-btn");

const resultCard = document.getElementById("result-card");
const videoThumb = document.getElementById("video-thumb");
const videoTitle = document.getElementById("video-title");
const videoAuthor = document.getElementById("video-author");
const videoViews = document.getElementById("video-views");
const downloadBtn = document.getElementById("download-btn");

// =====================================================================
// SINGLE VIDEO EXTRACTION
// =====================================================================
if (singleForm) {
  singleForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    
    const inputVal = urlInput.value.trim();
    if (!inputVal) return;

    currentOriginalUrl = inputVal;
    setLoadingState(true);

    try {
      const response = await fetch(`${API_BASE_URL}/api/download-single`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: inputVal }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || "Failed to fetch video details.");
      }

      // Render video metadata
      videoTitle.textContent = data.title || "TikTok Video";
      videoAuthor.textContent = data.author || "@creator";
      videoViews.textContent = `${data.views || "N/A"} views`;
      videoThumb.src = data.thumbnail || "";

      // Clean filename for the download header
      const safeFilename = (data.title || "tiktok_video")
        .replace(/[^a-zA-Z0-9_-]/g, "_")
        .substring(0, 30) + ".mp4";

      // Pass ORIGINAL URL to proxy-download to avoid CDN IP locks (403 Forbidden)
      const proxyUrl = `${API_BASE_URL}/api/proxy-download?url=${encodeURIComponent(
        currentOriginalUrl
      )}&filename=${encodeURIComponent(safeFilename)}`;

      // Attach download URL and filename to button attributes
      downloadBtn.dataset.downloadUrl = proxyUrl;
      downloadBtn.dataset.filename = safeFilename;

      if (resultCard) {
        resultCard.style.display = "block";
        resultCard.scrollIntoView({ behavior: "smooth" });
      }

    } catch (err) {
      alert(err.message || "An error occurred while fetching the video.");
    } finally {
      setLoadingState(false);
    }
  });
}

// =====================================================================
// DOWNLOAD BUTTON HANDLER
// =====================================================================
if (downloadBtn) {
  downloadBtn.addEventListener("click", async (e) => {
    e.preventDefault();

    const proxyUrl = downloadBtn.dataset.downloadUrl;
    const filename = downloadBtn.dataset.filename || "tiktok_video.mp4";

    if (!proxyUrl) {
      alert("Download link is missing. Please try processing the video again.");
      return;
    }

    const originalText = downloadBtn.innerHTML;
    downloadBtn.disabled = true;
    downloadBtn.innerHTML = `<span class="spinner"></span> Downloading...`;

    try {
      // Trigger streaming proxy request
      const res = await fetch(proxyUrl);

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `Server returned HTTP ${res.status}`);
      }

      // Stream binary blob to browser
      const blob = await res.blob();
      const blobUrl = window.URL.createObjectURL(blob);

      // Create temporary invisible link to trigger browser native download dialog
      const tempLink = document.createElement("a");
      tempLink.href = blobUrl;
      tempLink.download = filename;
      document.body.appendChild(tempLink);
      tempLink.click();

      // Clean up DOM and object URL memory
      document.body.removeChild(tempLink);
      window.URL.revokeObjectURL(blobUrl);

    } catch (err) {
      alert(`Download failed: ${err.message}`);
    } finally {
      downloadBtn.disabled = false;
      downloadBtn.innerHTML = originalText;
    }
  });
}

// =====================================================================
// HELPER UTILITIES
// =====================================================================
function setLoadingState(isLoading) {
  if (!singleBtn) return;
  if (isLoading) {
    singleBtn.disabled = true;
    singleBtn.dataset.originalText = singleBtn.innerHTML;
    singleBtn.innerHTML = `<span class="spinner"></span> Processing...`;
  } else {
    singleBtn.disabled = false;
    singleBtn.innerHTML = singleBtn.dataset.originalText || "Download Video";
  }
}