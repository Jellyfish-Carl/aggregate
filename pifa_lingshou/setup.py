from setuptools import setup, find_packages


setup(
    name="pifa-lingshou",
    version="0.1.0",
    description="Joint wholesale-retail aggregate optimization",
    packages=['pifa_lingshou']+['pifa_lingshou.'+m for m in find_packages('.',exclude=['tests','tests.*','resource','resource.*'])],
    package_dir={'pifa_lingshou':'.'},
    python_requires=">=3.9",
    package_data={"pifa_lingshou.utils.report": ["*.html", "*.css", "*.js"],
                  "pifa_lingshou.web": ["*.html", "*.js", "*.css"]},
    entry_points={'console_scripts':['pifa-lingshou=pifa_lingshou.web.server:main']},
    install_requires=["scipy>=1.11,<1.14"],
)
