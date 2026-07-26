// Initialize Lucide Icons
lucide.createIcons();

// Configuration
const API_BASE_URL = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1' 
    ? "http://127.0.0.1:8000/api" 
    : "https://savetokhd-app.onrender.com/api";

// Health Check on Load
async function checkBackendHealth() {
    try {
        const response = await fetch(`${API_BASE_URL}/health`);
        const data = await response.json();
        console.log("Backend Status:", data.status);
    } catch (err) {
        console.warn("Backend is offline or unreachable:", err);
    }
}
checkBackendHealth();

// State management
let currentBulkData = null;

// UI Elements
const elements = {
    tabSingle: document.getElementById('tab-single'),
    tabBulk: document.getElementById('tab-bulk'),
    formSingle: document.getElementById('form-single'),
    formBulk: document.getElementById('form-bulk'),
    inputSingle: document.getElementById('input-single'),
    inputBulk: document.getElementById('input-bulk'),
    btnSingle: document.getElementById('btn-single'),
    btnBulk: document.getElementById('btn-bulk'),
    loading: document.getElementById('loading'),
    loadingMsg: document.getElementById('loading-msg'),
    errorBox: document.getElementById('error-box'),
    errorMsg: document.getElementById('error-msg'),
    results: document.getElementById('results')
};

// --- Security Helpers ---

function escapeHTML(str) {
    if (!str) return "";
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// --- UI Logic ---

function switchTab(type) {
    if (type === 'single') {
        elements.tabSingle.classList.add('tab-active');
        elements.tabSingle.classList.remove('text-gray-400');
        elements.tabBulk.classList.remove('tab-active');
        elements.tabBulk.classList.add('text-gray-400');
        elements.formSingle.classList.remove('hidden');
        elements.formBulk.classList.add('hidden');
    } else {
        elements.tabBulk.classList.add('tab-active');
        elements.tabBulk.classList.remove('text-gray-400');
        elements.tabSingle.classList.remove('tab-active');
        elements.tabSingle.classList.add('text-gray-400');
        elements.formBulk.classList.remove('hidden');
        elements.formSingle.classList.add('hidden');
    }
    clearUI();
}

function showLoading(message) {
    elements.loading.classList.remove('hidden');
    elements.loadingMsg.innerText = message;
    elements.errorBox.classList.add('hidden');
    elements.results.classList.add('hidden');
}

function hideLoading() {
    elements.loading.classList.add('hidden');
}

function showError(message) {
    elements.errorBox.classList.remove('hidden');
    elements.errorMsg.innerText = message;
    hideLoading();
}

function clearUI() {
    elements.errorBox.classList.add('hidden');
    elements.results.classList.add('hidden');
    elements.results.innerHTML = '';
    currentBulkData = null;
}

function toggleFaq(button) {
    const item = button.parentElement;
    const isActive = item.classList.contains('accordion-active');
    document.querySelectorAll('.faq-item').forEach(el => el.classList.remove('accordion-active'));
    if (!isActive) item.classList.add('accordion-active');
}

// --- API Integration ---

async function handleSingleDownload() {
    const url = elements.inputSingle.value.trim();
    if (!url) return showError("Please enter a TikTok video URL.");
    if (!url.includes('tiktok.com')) return showError("Please enter a valid TikTok URL.");

    showLoading("Connecting to TikTok server...");
    
    try {
        const response = await fetch(`${API_BASE_URL}/download-single`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });
        
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || 'Failed to process video');
        }
        
        const data = await response.json();
        renderSingleResult(data);
    } catch (err) {
        showError(err.message || "Failed to fetch video. Please try again later.");
    } finally {
        hideLoading();
    }
}

/**
 * Triggers a real browser file save dialog by fetching the proxy stream 
 * as a Blob and creating an in-memory object URL.
 */
async function downloadFileViaBlob(proxyUrl, defaultFilename, btnElement) {
    const originalText = btnElement.innerHTML;
    try {
        // Show loading state on the button
        btnElement.style.pointerEvents = 'none';
        btnElement.innerHTML = `<i data-lucide="loader-2" class="w-5 h-5 animate-spin"></i> Downloading...`;
        lucide.createIcons();

        // 1. Fetch the video stream from our FastAPI backend proxy
        const response = await fetch(proxyUrl);
        if (!response.ok) {
            throw new Error(`Server returned HTTP ${response.status}`);
        }

        // 2. Convert incoming video bytes into a local Blob
        const blob = await response.blob();
        const blobUrl = window.URL.createObjectURL(blob);

        // 3. Programmatically create a hidden link and click it
        const tempLink = document.createElement('a');
        tempLink.href = blobUrl;
        tempLink.download = defaultFilename;
        document.body.appendChild(tempLink);
        tempLink.click();

        // 4. Clean up the object URL and remove element
        window.URL.revokeObjectURL(blobUrl);
        document.body.removeChild(tempLink);
    } catch (err) {
        console.error("Download failed:", err);
        alert("Failed to download video. Please try again.");
    } finally {
        // Restore button state
        btnElement.style.pointerEvents = 'auto';
        btnElement.innerHTML = originalText;
        lucide.createIcons();
    }
}

/**
 * Renders the single video result and routes the download through our proxy
 * endpoint using a Blob trigger to ensure the browser prompts the save dialog.
 */
function renderSingleResult(data) {
    elements.results.classList.remove('hidden');
    
    // Create a safe, clean file name
    const cleanTitle = (data.title || 'tiktok_video')
        .replace(/[^a-zA-Z0-9_-]/g, '_')
        .replace(/_+/g, '_')
        .slice(0, 30);
    const fileName = `${cleanTitle}.mp4`;
    
    // Construct the backend proxy URL that streams the file
    const proxyDownloadUrl = `${API_BASE_URL}/proxy-download?url=${encodeURIComponent(data.download_url)}&filename=${encodeURIComponent(fileName)}`;
    
    elements.results.innerHTML = `
        <div class="bg-gray-900 border border-gray-700 rounded-xl overflow-hidden flex flex-col md:flex-row">
            <div class="md:w-1/3 relative group">
                <img src="${escapeHTML(data.thumbnail)}" alt="Preview" class="w-full h-full object-cover">
                <div class="absolute inset-0 bg-black/40 flex items-center justify-center opacity-0 group-hover:opacity-100 transition">
                    <i data-lucide="play-circle" class="w-12 h-12 text-white"></i>
                </div>
            </div>
            <div class="p-6 md:w-2/3 flex flex-col justify-between">
                <div>
                    <h3 class="text-xl font-bold mb-2 line-clamp-2">${escapeHTML(data.title)}</h3>
                    <div class="flex gap-4 text-sm text-gray-400 mb-6">
                        <span class="flex items-center gap-1"><i data-lucide="user" class="w-4 h-4"></i> ${escapeHTML(data.author)}</span>
                        <span class="flex items-center gap-1"><i data-lucide="eye" class="w-4 h-4"></i> ${escapeHTML(data.views)}</span>
                    </div>
                </div>
                <div class="space-y-3">
                    <button type="button" 
                            class="w-full bg-green-600 hover:bg-green-700 py-3 rounded-lg font-bold text-center transition flex items-center justify-center gap-2 text-white cursor-pointer"
                            id="btn-download-media">
                        <i data-lucide="download"></i> Download No-Watermark MP4
                    </button>
                </div>
            </div>
        </div>
    `;
    lucide.createIcons();

    // Attach event listener to trigger download via Blob
    const downloadBtn = document.getElementById('btn-download-media');
    if (downloadBtn) {
        downloadBtn.addEventListener('click', () => {
            downloadFileViaBlob(proxyDownloadUrl, fileName, downloadBtn);
        });
    }
}

async function handleBulkExtract() {
    const input = elements.inputBulk.value.trim();
    if (!input) return showError("Please enter a profile URL or username.");

    showLoading("Fetching profile metadata...");
    
    try {
        const response = await fetch(`${API_BASE_URL}/extract-bulk`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                username: input.replace('@', ''), 
                delay: 1.0 
            })
        });
        
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || 'Bulk extraction failed');
        }
        
        const data = await response.json();
        currentBulkData = data;
        renderBulkResult(data);
    } catch (err) {
        showError(err.message || "Failed to extract profile. Ensure the account is public.");
    } finally {
        hideLoading();
    }
}

function renderBulkResult(data) {
    elements.results.classList.remove('hidden');
    elements.results.innerHTML = `
        <div class="space-y-6">
            <div class="flex flex-col md:flex-row justify-between items-start md:items-center gap-4 bg-gray-900 p-4 rounded-xl border border-gray-700">
                <div>
                    <h3 class="text-lg font-bold">Extraction Summary: ${escapeHTML(data.username)}</h3>
                    <p class="text-gray-400 text-sm">Found ${data.total_videos} videos ready for download.</p>
                </div>
                <button id="btn-export" class="bg-gray-100 text-gray-900 hover:bg-white px-6 py-2 rounded-lg font-bold flex items-center gap-2 transition">
                    <i data-lucide="file-spreadsheet"></i> Export as CSV
                </button>
            </div>

            <div class="overflow-x-auto rounded-xl border border-gray-700">
                <table class="w-full text-left border-collapse">
                    <thead class="bg-gray-800 text-gray-400 text-xs uppercase tracking-wider">
                        <tr>
                            <th class="p-4">Video Caption</th>
                            <th class="p-4">Views</th>
                            <th class="p-4">Duration</th>
                            <th class="p-4 text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-800">
                        ${data.videos.map(v => `
                            <tr class="hover:bg-gray-800/50 transition">
                                <td class="p-4 font-medium max-w-xs truncate" title="${escapeHTML(v.caption)}">${escapeHTML(v.caption)}</td>
                                <td class="p-4 text-gray-400">${escapeHTML(v.views)}</td>
                                <td class="p-4 text-gray-400">${escapeHTML(v.duration)}</td>
                                <td class="p-4 text-right">
                                    <a href="${escapeHTML(v.url)}" target="_blank" class="text-red-500 hover:text-red-400 font-bold inline-flex items-center gap-1">
                                        <i data-lucide="external-link" class="w-4 h-4"></i> View
                                    </a>
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        </div>
    `;
    lucide.createIcons();
    
    document.getElementById('btn-export').addEventListener('click', exportToCSV);
}

// --- CSV Export Logic ---

function exportToCSV() {
    if (!currentBulkData || !currentBulkData.videos.length) return;

    const headers = ["Caption", "Views", "Duration", "TikTok URL"];
    
    const rows = currentBulkData.videos.map(v => [
        `"${v.caption.replace(/"/g, '""')}"`,
        `"${v.views}"`,
        `"${v.duration}"`,
        `"${v.url}"`
    ]);

    const csvContent = [headers.join(","), ...rows.map(r => r.join(","))].join("\n");
    
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    
    const filename = `tiktok_export_${currentBulkData.username.replace('@', '')}_${new Date().getTime()}.csv`;
    
    link.setAttribute("href", url);
    link.setAttribute("download", filename);
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

// --- Event Listeners ---

if (elements.tabSingle) elements.tabSingle.addEventListener('click', () => switchTab('single'));
if (elements.tabBulk) elements.tabBulk.addEventListener('click', () => switchTab('bulk'));

elements.btnSingle.addEventListener('click', handleSingleDownload);
elements.btnBulk.addEventListener('click', handleBulkExtract);
elements.inputSingle.addEventListener('keypress', (e) => { if (e.key === 'Enter') handleSingleDownload(); });
elements.inputBulk.addEventListener('keypress', (e) => { if (e.key === 'Enter') handleBulkExtract(); });