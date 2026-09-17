# Replaces the contributed astropy hook, which collects every submodule and
# dies on astropy.visualization.wcsaxes: that module calls
# pytest.importorskip("matplotlib") at import time, and matplotlib is not a
# dependency here. Astrodoro uses coordinates, time, io.fits and units.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

datas = collect_data_files("astropy")
datas += copy_metadata("astropy")
datas += copy_metadata("numpy")
datas += [
    (path, target)
    for path, target in collect_data_files("astropy", include_py_files=True)
    if path.endswith(("_parsetab.py", "_lextab.py"))
]

hiddenimports = ["numpy.lib.recfunctions"] + collect_submodules(
    "astropy", filter=lambda name: not name.startswith("astropy.visualization")
)
