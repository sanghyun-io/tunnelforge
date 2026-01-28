#!/usr/bin/env python3
"""Convert SVG icon to PNG and ICO formats using PyMuPDF."""

import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
    from PIL import Image
except ImportError as e:
    print(f"Error: Missing dependency - {e}")
    print("Install with: pip install pymupdf pillow")
    sys.exit(1)


def convert_svg_to_png(svg_path: Path, png_path: Path, size: int):
    """Convert SVG to PNG at specified size using PyMuPDF."""
    # Read SVG content
    svg_content = svg_path.read_bytes()

    # Open SVG with PyMuPDF
    doc = fitz.open(stream=svg_content, filetype="svg")
    page = doc[0]

    # Calculate zoom factor
    zoom = size / max(page.rect.width, page.rect.height)
    mat = fitz.Matrix(zoom, zoom)

    # Render to pixmap
    pix = page.get_pixmap(matrix=mat, alpha=True)

    # Save as PNG
    pix.save(str(png_path))
    doc.close()

    print(f"  Created: {png_path.name} ({size}x{size})")


def create_ico(png_paths: list, ico_path: Path):
    """Create ICO file from multiple PNG files."""
    images = []
    for png_path in png_paths:
        if png_path and png_path.exists():
            img = Image.open(png_path)
            # Ensure RGBA mode
            if img.mode != 'RGBA':
                img = img.convert('RGBA')
            images.append(img)

    if not images:
        print("  Warning: No PNG files found for ICO creation")
        return

    # Sort by size (largest first for best quality)
    images.sort(key=lambda x: x.width, reverse=True)

    # Save as ICO with multiple sizes
    images[0].save(
        ico_path,
        format='ICO',
        sizes=[(img.width, img.height) for img in images],
        append_images=images[1:]
    )
    print(f"  Created: {ico_path.name} (multi-size)")


def main():
    # Paths
    project_root = Path(__file__).parent.parent
    assets_dir = project_root / "assets"
    svg_path = assets_dir / "icon.svg"

    if not svg_path.exists():
        print(f"Error: SVG not found at {svg_path}")
        sys.exit(1)

    print("Converting TunnelForge icon...")
    print(f"Source: {svg_path}")
    print()

    # Create PNG files at various sizes
    sizes = [16, 32, 48, 64, 128, 256, 512]
    temp_pngs = {}

    print("Creating PNG files:")
    for size in sizes:
        png_path = assets_dir / f"icon_{size}.png"
        try:
            convert_svg_to_png(svg_path, png_path, size)
            temp_pngs[size] = png_path
        except Exception as e:
            print(f"  Warning: Failed to create {size}x{size}: {e}")

    # Finalize PNG files
    print()
    print("Finalizing PNG files:")

    icon_png = assets_dir / "icon.png"
    if 256 in temp_pngs and temp_pngs[256].exists():
        # Copy 256px version to icon.png
        import shutil
        shutil.copy(temp_pngs[256], icon_png)
        print(f"  Copied: icon.png (256x256)")

    icon_512 = assets_dir / "icon_512.png"
    temp_512 = assets_dir / "icon_512.png"
    if 512 in temp_pngs and temp_pngs[512].exists():
        # Already created with correct name
        print(f"  Ready: icon_512.png (512x512)")

    print()
    print("Creating ICO file:")

    # Create ICO with sizes: 16, 32, 48, 64, 128, 256
    ico_sizes = [16, 32, 48, 64, 128, 256]
    ico_pngs = [temp_pngs.get(s) for s in ico_sizes if s in temp_pngs]
    ico_path = assets_dir / "icon.ico"
    create_ico(ico_pngs, ico_path)

    # Clean up intermediate PNGs (keep icon_512.png)
    print()
    print("Cleaning up intermediate files:")
    for size in [16, 32, 48, 64, 128, 256]:  # Don't delete 512
        temp_png = assets_dir / f"icon_{size}.png"
        if temp_png.exists():
            temp_png.unlink()
            print(f"  Removed: icon_{size}.png")

    print()
    print("=" * 40)
    print("Done! Final icon files:")
    print("=" * 40)
    for f in ["icon.svg", "icon.ico", "icon.png", "icon_512.png"]:
        file_path = assets_dir / f
        if file_path.exists():
            size_kb = file_path.stat().st_size / 1024
            print(f"  {f:<15} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
