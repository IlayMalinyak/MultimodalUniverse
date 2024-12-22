import os
import argparse
import numpy as np
from astropy.io import fits
from astropy.table import Table, join
from multiprocessing import Pool
from tqdm import tqdm
import h5py
import healpy as hp
import re
import pandas as pd

_healpix_nside = 16

# Breakdown of the different Kepler pipelines

def convert_to_list(string_list:str):
    """
    Convert a string representation of a list to a list.

    Args:
        string_list (str): The string representation of the list.

    Returns:
        List: The list.
    """
    # Extract content within square brackets
    matches = re.findall(r'\[(.*?)\]', string_list)
    if matches:
        # Split by comma, remove extra characters except period, hyphen, underscore, and comma, and strip single quotes
        cleaned_list = [re.sub(r'[^A-Za-z0-9\-/_,.]', '', s) for s in matches[0].split(',')]
        return cleaned_list
    else:
        return []
    
def convert_ints_to_list(string:str):
    """
    Convert a string representation of a list of integers to a list of integers.

    Args:
        string (str): The string representation of the list of integers.

    Returns:
        List: The list of integers.
    """
    values = string.strip('[]').split(',')
    return [int(value) for value in values]

def processing_fn(args):
    """ Parallel processing function reading all requested light curves.
    """
    filenames, object_id = args
    n_quarters = len(filenames) # Number of quarters
    sap_fluxes = []
    pdcsap_fluxes = []
    times = []
    sap_fluxes_errs = []
    pdcsap_fluxes_errs = []
    for i, filename in enumerate(filenames):
        with fits.open(filename, mode='readonly', memmap=True) as hdu:
            # Kepler header parsing
            telescope = hdu[0].header.get('TELESCOP')
            if telescope == 'Kepler' and hdu[0].header.get('ORIGIN') == 'NASA/Ames':
                # Kepler-specific header information extraction
                targetid = hdu[0].header.get('KEPLERID')
                assert targetid == object_id, "Target ID mismatch"
                ra = hdu[0].header.get('RA_OBJ')
                dec = hdu[0].header.get('DEC_OBJ')
                obsmode = hdu[0].header.get('OBSMODE')

                # Time handling for Kepler (Kepler Julian Date)
                time = hdu[1].data['TIME']
                # time_format = hdu[1].data['TUNIT1']
                # Units: Kepler Julian Date
                # Two flux options: SAP (Simple Aperture Photometry) and PDCSAP (Pre-Search Data Conditioning)
                sap_flux = hdu[1].data['SAP_FLUX']
                sap_flux_err = hdu[1].data['SAP_FLUX_ERR']
                pdcsap_flux = hdu[1].data['PDCSAP_FLUX']
                pdcsap_flux_err = hdu[1].data['PDCSAP_FLUX_ERR']
                # Quality flags for Kepler
                quality = np.asarray(hdu[1].data['SAP_QUALITY'], dtype='int32')
                good_data_mask = (quality == 0) & \
                                np.isfinite(time) & \
                                np.isfinite(sap_flux) & \
                                np.isfinite(sap_flux_err) & \
                                np.isfinite(pdcsap_flux) & \
                                np.isfinite(pdcsap_flux_err)

                times.append(time[good_data_mask])
                sap_fluxes.append(sap_flux[good_data_mask])
                pdcsap_fluxes.append(pdcsap_flux[good_data_mask])
                sap_fluxes_errs.append(sap_flux_err[good_data_mask])
                pdcsap_fluxes_errs.append(pdcsap_flux_err[good_data_mask])

                

    # Basic quality filtering
    sap_fluxes = normalize_lightcurve(sap_fluxes)
    pdcsap_fluxes = normalize_lightcurve(pdcsap_fluxes)
    
    # Combine times and normalized light curves
    times = np.concatenate(times)
    sap_fluxes = np.concatenate(sap_fluxes)
    pdcsap_fluxes = np.concatenate(pdcsap_fluxes)
    sap_fluxes_errs = np.concatenate(sap_fluxes_errs)
    pdcsap_fluxes_errs = np.concatenate(pdcsap_fluxes_errs)

    # Return the results
    return {
        'object_id': object_id,
        'time': times,
        'sap_flux': sap_fluxes,
        'sap_flux_err': sap_flux_err,
        'pdcsap_flux': pdcsap_fluxes,
        'pdcsap_flux_err': pdcsap_flux_err,
        'ra': ra,
        'dec': dec,
        'cadence': obsmode
    }


def normalize_lightcurve(lc):
        # Combine all quarter's light curves
        combined_lc = np.concatenate(lc)
        # Calculate the mean of the combined light curve
        global_mean = np.nanmean(combined_lc)
        
        # Normalize each quarter's light curve
        normalized_lc = []
        for quarter_lc in lc:
            quarter_mean = np.nanmean(quarter_lc)
            normalized_quarter = quarter_lc * (global_mean / quarter_mean)
            normalized_lc.append(normalized_quarter)
        return normalized_lc


def save_in_standard_format(args):
    """ Process Kepler light curves and save in standard format.
    """

    catalog, output_filename, kepler_data_path, tiny = args

    # Create the output directory if it does not exist
    if not os.path.exists(os.path.dirname(output_filename)):
        os.makedirs(os.path.dirname(output_filename))

    # Rename columns to match the standard format
    catalog['object_id'] = catalog['KID']

    # Process all files
    results = []
    for args in catalog[['data_file_path', 'object_id']]:
        results.append(processing_fn(args))
    
    # Pad all light curves to the same length
    max_length = max([len(d['time']) for d in results])
    for i in range(len(results)):
        results[i]['time'] = np.pad(results[i]['time'], (0, max_length - len(results[i]['time'])), mode='constant',
                                    constant_values=np.nan)
        results[i]['sap_flux'] = np.pad(results[i]['sap_flux'], (0, max_length - len(results[i]['sap_flux'])), mode='constant',
                                    constant_values=np.nan)
        results[i]['sap_flux_err'] = np.pad(results[i]['sap_flux_err'], (0, max_length - len(results[i]['sap_flux_err'])),
                                        mode='constant', constant_values=np.nan)
        results[i]['pdcsap_flux'] = np.pad(results[i]['pdcsap_flux'], (0, max_length - len(results[i]['pdcsap_flux'])),
                                        mode='constant',
                                        constant_values=np.nan)
        results[i]['pdcsap_flux_err'] = np.pad(results[i]['pdcsap_flux_err'],
                                            (0, max_length - len(results[i]['pdcsap_flux_err'])),
                                            mode='constant', constant_values=np.nan)

    # Aggregate all light curves into an astropy table
    lightcurves = Table({k: [d[k] for d in results]
                         for k in results[0].keys()})
    for key in catalog.colnames:
        if isinstance(catalog[key][0], list):
            catalog[key] = [','.join(map(str, item)) for item in catalog[key]]

    # Join on target id with the input catalog
    catalog = join(catalog, lightcurves, keys='object_id', join_type='inner')
    catalog.convert_unicode_to_bytestring()

    # Making sure we didn't lose anyone
    assert len(catalog) == len(
        lightcurves), "There was an error in the join operation, probably some light curve files are missing"

    # Save all columns to disk in HDF5 format
    with h5py.File(output_filename, 'w') as hdf5_file:
        for key in catalog.colnames:
            hdf5_file.create_dataset(key, data=catalog[key])
    return 1


def main(args):
    # Load the catalog file
    catalog = pd.read_csv(os.path.join(args.kepler_data_path, f"all_kepler_samples.csv"))
    catalog = catalog.loc[:, ~catalog.columns.str.contains('^Unnamed')]
    catalog['data_file_path'] = catalog['data_file_path'].apply(convert_to_list) # file paths is a string, convert to list
    catalog['qs'] = catalog['qs'].apply(convert_ints_to_list) # quarters is a string, convert to list
    print("Catalog loaded: ", catalog.columns)
    if args.tiny:
        catalog = catalog.iloc[:10]
    catalog = Table.from_pandas(catalog)

    # Add healpix index to the catalog
    catalog['healpix'] = hp.ang2pix(_healpix_nside, catalog['RA_OBJ'], catalog['DEC_OBJ'], lonlat=True, nest=True)

    catalog = catalog.group_by(['healpix'])

    map_args = []
    for group in catalog.groups:
        # Create a filename for the group
        group_filename = os.path.join(args.output_dir,
                                        'healpix={}/001-of-001.hdf5'.format(group['healpix'][0]))
        map_args.append((group, group_filename, args.kepler_data_path, args.tiny))
    # Run the parallel processing
    with Pool(args.num_processes) as pool:
        results = list(tqdm(pool.imap(save_in_standard_format, map_args), total=len(map_args)))

    if sum(results) != len(map_args):
        print("There was an error in the parallel processing, some files may not have been processed correctly")

    print("All done!")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Extracts light curves from Kepler data downloaded from MAST')
    parser.add_argument('kepler_data_path', type=str, help='Path to the local copy of the Kepler data')
    parser.add_argument('output_dir', type=str, help='Path to the output directory')
    parser.add_argument('-qs', '--quarters', type=int, help='Kepler Quarters to process', default=17)
    parser.add_argument('-nproc', '--num_processes', type=int, default=10,
                        help='The number of processes to use for parallel processing')
    parser.add_argument('--tiny', action='store_true', help='Use a tiny subset of the data for testing')
    args = parser.parse_args()

    main(args)