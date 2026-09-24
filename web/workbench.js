/* Native helpers activate only inside the standalone application. */
(() => {
  const guide = document.getElementById('quickGuide');
  document.getElementById('openGuide').addEventListener('click', () => guide.showModal());
  document.getElementById('closeGuide').addEventListener('click', () => guide.close());
  guide.addEventListener('click', event => { if (event.target === guide) guide.close(); });
  document.addEventListener('keydown', event => {
    if (event.key === 'F1') { event.preventDefault(); if (!guide.open) guide.showModal(); }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'o' && !guide.open) {
      const picker = document.getElementById('dataFile');
      if (!document.getElementById('configuration').disabled && !picker.closest('[hidden]')) {
        event.preventDefault(); picker.click();
      }
    }
  });
  let attached = false;
  async function attachDesktop() {
    if (attached || !window.pywebview?.api) return;
    attached = true;
    document.body.classList.add('desktop-app');
    document.querySelector('.version').textContent = '桌面版 · v0.7.0';
    document.getElementById('workspaceBadge').innerHTML = '<span class="dot"></span>桌面工作台';
    document.getElementById('desktopFolder').hidden = false;
    try {
      const info = await window.pywebview.api.runtime_info();
      document.getElementById('guideDataPath').textContent = info.data_dir;
      window.paramidDataDirectory = info.data_dir;
    } catch (error) { status('读取实验目录失败：' + error, true); }
  }
  document.getElementById('desktopFolder').addEventListener('click', async () => {
    try { await window.pywebview.api.open_data_folder(); }
    catch (error) { status('打开实验目录失败：' + error, true); }
  });
  window.addEventListener('pywebviewready', attachDesktop);
  attachDesktop();
})();
