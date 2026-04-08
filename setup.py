from setuptools import setup

setup(
    name="opcua-device-simulator",
    version="1.0.0",
    description="OPC-UA server simulating Opto22 groov RIO (CODESYS 3.5), Siemens S7-1200, and Unitronics PLC node trees",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Mitch Sowa",
    url="https://github.com/mitchsowa/opcua-device-simulator",
    license="MIT",
    python_requires=">=3.9",
    py_modules=["opcua_sim"],
    install_requires=[
        "asyncua>=1.1.0",
    ],
    entry_points={
        "console_scripts": [
            "opcua-sim=opcua_sim:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "Intended Audience :: Manufacturing",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Scientific/Engineering :: Interface Engine/Protocol Translator",
        "Topic :: Software Development :: Testing",
        "Topic :: System :: Hardware",
    ],
    keywords=[
        "opc-ua", "opcua", "plc", "scada", "industrial", "automation",
        "siemens", "opto22", "unitronics", "codesys", "simulator",
    ],
)
