# -*- coding: utf-8 -*-
# Copyright (C) Duncan Macleod (2013)
#
# This file is part of GWpy.
#
# GWpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# GWpy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GWpy.  If not, see <http://www.gnu.org/licenses/>.

"""Read and write HDF5 files in the LIGO Open Science Center format

For more details, see https://losc.ligo.org
"""

from __future__ import print_function

import os.path
from math import ceil

from six.moves.urllib.parse import urlparse

from astropy.io import registry
from astropy.units import Quantity
from astropy.utils.data import get_readable_fileobj

from .. import (StateVector, TimeSeries)
from ...io import hdf5 as io_hdf5
from ...io.cache import file_segment
from ...detector.units import parse_unit
from ...segments import Segment
from ...time import to_gps
from ...utils.env import bool_env


# -- utilities ----------------------------------------------------------------

def _download_file(url, cache=None, verbose=False):
    if cache is None:
        cache = bool_env('GWPY_CACHE', False)
    return get_readable_fileobj(url, cache=cache, show_progress=verbose)


def _fetch_losc_data_file(url, *args, **kwargs):
    """Internal function for fetching a single LOSC file and returning a Series
    """
    cls = kwargs.pop('cls', TimeSeries)
    cache = kwargs.pop('cache', None)
    verbose = kwargs.pop('verbose', False)

    # match file format
    if url.endswith('.gz'):
        ext = os.path.splitext(url[:-3])[-1]
    else:
        ext = os.path.splitext(url)[-1]
    if ext == '.hdf5':
        kwargs.setdefault('format', 'hdf5.losc')
    elif ext == '.txt':
        kwargs.setdefault('format', 'ascii.losc')
    elif ext == '.gwf':
        kwargs.setdefault('format', 'gwf')

    with _download_file(url, cache, verbose=verbose) as rem:
        if verbose:
            print('Reading data...', end=' ')
        try:
            series = cls.read(rem, *args, **kwargs)
        except Exception as exc:
            if verbose:
                print('')
            exc.args = ("Failed to read LOSC data from %r: %s"
                        % (url, str(exc)),)
            raise
        else:
            # parse bits from unit in GWF
            if ext == '.gwf' and isinstance(series, StateVector):
                try:
                    bits = {}
                    for bit in str(series.unit).split():
                        a, b = bit.split(':', 1)
                        bits[int(a)] = b
                    series.bits = bits
                    series.override_unit('')
                except (TypeError, ValueError):  # don't care, bad LOSC
                    pass

            if verbose:
                print('[Done]')
            return series


# -- remote data access (the main event) --------------------------------------

def fetch_losc_data(detector, start, end, cls=TimeSeries, **kwargs):
    """Fetch LOSC data for a given detector

    This function is for internal purposes only, all users should instead
    use the interface provided by `TimeSeries.fetch_open_data` (and similar
    for `StateVector.fetch_open_data`).
    """
    from gwosc.locate import get_urls

    # format arguments
    start = to_gps(start)
    end = to_gps(end)
    span = Segment(start, end)
    kwargs.update({
        'start': start,
        'end': end,
    })

    # find URLs (requires gwopensci)
    url_kw = {key: kwargs.pop(key) for key in
              ('sample_rate', 'tag', 'version', 'host', 'format') if
              key in kwargs}
    if 'sample_rate' in url_kw:  # format as Hertz
        url_kw['sample_rate'] = Quantity(url_kw['sample_rate'], 'Hz').value
    cache = get_urls(detector, int(start), int(ceil(end)), **url_kw)
    if kwargs.get('verbose', False):  # get_urls() guarantees len(cache) >= 1
        host = urlparse(cache[0]).netloc
        print("Fetched {0!d} URLs from {1} for [{2!d} .. {3!d})".format(
            len(cache), host, int(start), int(end)))

    # if event dataset, pick shortest file that covers the request
    if len(cache) and 'events' in cache[0]:
        for url in cache:
            a, b = file_segment(url)
            if a <= start and b >= end:
                cache = [url]
                break
    if len(cache) and cache[0].endswith('.gwf'):
        try:
            args = (kwargs.pop('channel'),)
        except KeyError:  # no specified channel
            if cls is StateVector:
                args = ('{}:LOSC-DQMASK'.format(detector,),)
            else:
                args = ('{}:LOSC-STRAIN'.format(detector,),)
    else:
        args = ()

    # read data
    out = None
    kwargs['cls'] = cls
    for url in cache:
        keep = file_segment(url) & span
        print(url)
        new = _fetch_losc_data_file(url, *args, **kwargs).crop(
            *keep, copy=False)
        if out is None:
            out = new.copy()
        else:
            out.append(new, resize=True)
    return out


# -- I/O ----------------------------------------------------------------------

@io_hdf5.with_read_hdf5
def read_losc_hdf5(h5f, path='strain/Strain',
                   start=None, end=None, copy=False):
    """Read a `TimeSeries` from a LOSC-format HDF file.

    Parameters
    ----------
    h5f : `str`, `h5py.HLObject`
        path of HDF5 file, or open `H5File`

    path : `str`
        name of HDF5 dataset to read.

    Returns
    -------
    data : `~gwpy.timeseries.TimeSeries`
        a new `TimeSeries` containing the data read from disk
    """
    dataset = io_hdf5.find_dataset(h5f, path)
    # read data
    nddata = dataset.value
    # read metadata
    xunit = parse_unit(dataset.attrs['Xunits'])
    epoch = dataset.attrs['Xstart']
    dt = Quantity(dataset.attrs['Xspacing'], xunit)
    unit = dataset.attrs['Yunits']
    # build and return
    return TimeSeries(nddata, epoch=epoch, sample_rate=(1/dt).to('Hertz'),
                      unit=unit, name=path.rsplit('/', 1)[1],
                      copy=copy).crop(start=start, end=end)


@io_hdf5.with_read_hdf5
def read_losc_hdf5_state(f, path='quality/simple', start=None, end=None,
                         copy=False):
    """Read a `StateVector` from a LOSC-format HDF file.

    Parameters
    ----------
    f : `str`, `h5py.HLObject`
        path of HDF5 file, or open `H5File`

    path : `str`
        path of HDF5 dataset to read.

    start : `Time`, `~gwpy.time.LIGOTimeGPS`, optional
        start GPS time of desired data

    end : `Time`, `~gwpy.time.LIGOTimeGPS`, optional
        end GPS time of desired data

    copy : `bool`, default: `False`
        create a fresh-memory copy of the underlying array

    Returns
    -------
    data : `~gwpy.timeseries.TimeSeries`
        a new `TimeSeries` containing the data read from disk
    """
    # find data
    dataset = io_hdf5.find_dataset(f, '%s/DQmask' % path)
    maskset = io_hdf5.find_dataset(f, '%s/DQDescriptions' % path)
    # read data
    nddata = dataset.value
    bits = [bytes.decode(bytes(b), 'utf-8') for b in maskset.value]
    # read metadata
    epoch = dataset.attrs['Xstart']
    try:
        dt = dataset.attrs['Xspacing']
    except KeyError:
        dt = Quantity(1, 's')
    else:
        xunit = parse_unit(dataset.attrs['Xunits'])
        dt = Quantity(dt, xunit)
    return StateVector(nddata, bits=bits, epoch=epoch, name='Data quality',
                       dx=dt, copy=copy).crop(start=start, end=end)


# register
registry.register_reader('hdf5.losc', TimeSeries, read_losc_hdf5)
registry.register_reader('hdf5.losc', StateVector, read_losc_hdf5_state)
