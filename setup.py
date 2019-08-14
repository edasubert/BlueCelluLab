#!/usr/bin/env python
""" BGLibPy setup """

import setuptools
import versioneer

setuptools.setup(
    name="bglibpy",
    version=versioneer.get_version(),
    cmdclass=versioneer.get_cmdclass(),
    packages=setuptools.find_packages(exclude=('examples', )),
    author="Werner Van Geit",
    author_email="werner.vangeit@epfl.ch",
    description="The Pythonic Blue Brain simulator access",
    license="BBP-internal-confidential",
    dependency_links=["https://bbpteam.epfl.ch/repository/devpi/bbprelman/"
                      "dev/+simple/bluepy/"],
    extras_require={"bbp": ["bluepy[bbp]>=0.11.10.dev1", "brain"]},
    install_requires=["bluepy>=0.12", "libsonata",
                      "bluepy-configfile>=0.1.2.dev1", "h5py"],
    tests_require=["bluepy[bbp]>=0.12", "brain"],
    keywords=(
        'computational neuroscience',
        'simulation',
        'analysis',
        'parameters',
        'Blue Brain Project'),
    url="http://bbpteam.epfl.ch/project/issues/projects/BGLPY",
    download_url="https://bbpcode.epfl.ch/code/#/admin/projects/sim/BGLibPy",
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Console',
        'License :: Proprietary',
        'Operating System :: POSIX',
        'Topic :: Scientific/Engineering',
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 3',
        'Topic :: Utilities',
    ],
)
