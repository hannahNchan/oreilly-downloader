import re
from pathlib import Path
from typing import Callable

from .base import Plugin


class AssetsPlugin(Plugin):
    # Saltar lo que ya existe es lo que permite reanudar una descarga cortada.
    # Para que eso sea seguro, la escritura tiene que ser atomica: si el proceso
    # muere a mitad de un write, el archivo truncado quedaria en disco y el
    # reintento lo daria por bueno. Se escribe a .part y se renombra.

    def download_image(self, url: str, save_path: Path) -> bool:
        if save_path.exists():
            return True

        save_path.parent.mkdir(parents=True, exist_ok=True)
        content = self.http.get_bytes(url)
        tmp = save_path.parent / (save_path.name + ".part")
        tmp.write_bytes(content)
        tmp.replace(save_path)
        return True

    def download_css(self, url: str, save_path: Path) -> bool:
        if save_path.exists():
            return True

        save_path.parent.mkdir(parents=True, exist_ok=True)
        content = self.http.get_text(url)
        tmp = save_path.parent / (save_path.name + ".part")
        tmp.write_text(content, encoding='utf-8')
        tmp.replace(save_path)
        return True

    def download_all_images(
        self,
        urls: list[str],
        output_dir: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, Path]:
        downloaded = {}
        failed = []
        total = len(urls)
        for i, url in enumerate(urls):
            filename = url.split("/")[-1]
            save_path = output_dir / "Images" / filename
            try:
                self.download_image(url, save_path)
                downloaded[url] = save_path
            except Exception as e:
                # A single unreachable/timed-out image must not abort the whole
                # book. Skip it, keep going, and report the count at the end.
                failed.append(url)
                print(f"[assets] skipping image after retries: {filename} ({e})")
            if progress_callback:
                progress_callback(i + 1, total)
        if failed:
            print(f"[assets] {len(failed)}/{total} images could not be downloaded and were skipped")
        return downloaded

    def download_all_css(
        self,
        urls: list[str],
        output_dir: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, Path]:
        downloaded = {}
        total = len(urls)
        for i, url in enumerate(urls):
            save_path = output_dir / "Styles" / f"Style{i:02d}.css"
            self.download_css(url, save_path)
            downloaded[url] = save_path
            if progress_callback:
                progress_callback(i + 1, total)
        return downloaded

    def download_css_assets(self, css_urls: list[str], oebps: Path):
        """Download assets referenced by url() in CSS files."""
        styles_dir = oebps / "Styles"
        if not styles_dir.exists():
            return

        for i, css_url in enumerate(css_urls):
            css_path = styles_dir / f"Style{i:02d}.css"
            if not css_path.exists():
                continue

            css_text = css_path.read_text(encoding="utf-8")
            for match in re.finditer(r'url\(["\']?([^)"\']+)["\']?\)', css_text):
                ref = match.group(1)
                if ref.startswith("data:") or ref.startswith("http"):
                    continue

                # Resolve relative to CSS file location, download from source
                save_path = (styles_dir / ref).resolve()
                if save_path.exists():
                    continue

                # Build download URL from the CSS source URL
                css_base = css_url.rsplit("/", 1)[0]
                asset_url = f"{css_base}/{ref}"
                try:
                    self.download_image(asset_url, save_path)
                except Exception:
                    pass

    def get_cover_url(self, book_id: str) -> str:
        return f"https://learning.oreilly.com/library/cover/{book_id}/"
