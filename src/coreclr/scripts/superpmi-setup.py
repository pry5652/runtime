#!/usr/bin/env python3
#
## Licensed to the .NET Foundation under one or more agreements.
## The .NET Foundation licenses this file to you under the MIT license.
#
##
# Title               : superpmi-setup.py
#
# Notes:
#
# Script to setup directory structure required to perform SuperPMI collection in CI.
# It does the following steps:
# 1.  It creates `correlation_payload_directory` that contains files from CORE_ROOT, src\coreclr\scripts.
#     This directory is the one that is sent to all the helix machines that performs SPMI collection.
# 2.  It clones dotnet/jitutils, builds it and then copies the `pmi.dll` to `correlation_payload_directory` folder.
#     This file is needed to do pmi SPMI runs.
# 3.  The script takes `input_artifacts` parameter which contains managed .dlls and .exes on
#     which SPMI needs to be run. This script will partition these folders into equal buckets of approximately `max_size`
#     bytes and stores them under `payload` directory. Each sub-folder inside `payload` directory is sent to individual
#     helix machine to do SPMI collection on. E.g. for `input_artifacts` to be run on libraries, the parameter would be path to
#     `CORE_ROOT` folder and this script will copy `max_size` bytes of those files under `payload/libraries/0/binaries`,
#     `payload/libraries/1/binaries` and so forth.
# 4.  Lastly, it sets the pipeline variables.

# Below are the helix queues it sets depending on the OS/architecture:
# | Arch  | Windows_NT       | Linux                                                                                                                                |
# |-------|------------------|--------------------------------------------------------------------------------------------------------------------------------------|
# | x86   | Windows.10.Amd64 | -                                                                                                                                    |
# | x64   | Windows.10.Amd64 | Ubuntu.1804.Amd64                                                                                                                    |
# | arm   | -                | (Ubuntu.1804.Arm32)Ubuntu.1804.Armarch@mcr.microsoft.com/dotnet-buildtools/prereqs:ubuntu-18.04-helix-arm32v7-bfcd90a-20200121150440 |
# | arm64 | Windows.10.Arm64 | (Ubuntu.1804.Arm64)Ubuntu.1804.ArmArch@mcr.microsoft.com/dotnet-buildtools/prereqs:ubuntu-18.04-helix-arm64v8-a45aeeb-20190620155855 |
################################################################################
################################################################################


import argparse
import shutil
import subprocess
import tempfile

from os import listdir, path, walk
from os.path import isfile, join, getsize
from coreclr_arguments import *

# Start of parser object creation.

parser = argparse.ArgumentParser(description="description")

parser.add_argument("-source_directory", help="path to source directory")
parser.add_argument("-core_root_directory", help="path to core_root directory")
parser.add_argument("-arch", help="Architecture")
parser.add_argument("-mch_file_tag", help="Tag to be used to mch files")
parser.add_argument("-collection_name", help="Name of the SPMI collection to be done")
parser.add_argument("-input_directory", help="directory containing assemblies for which superpmi collection to "
                                                  "be done")
parser.add_argument("-max_size", help="Max size of each partition in MB")
is_windows = platform.system() == "Windows"
native_binaries_to_ignore = [
    "clrcompression.dll",
    "clretwrc.dll",
    "clrgc.dll",
    "clrjit.dll",
    "clrjit_unix_arm_x64.dll",
    "clrjit_unix_arm64_x64.dll",
    "clrjit_unix_x64_x64.dll",
    "clrjit_win_arm_x64.dll",
    "clrjit_win_arm64_x64.dll",
    "clrjit_win_x64_x64.dll",
    "clrjit_win_x86_x64.dll",
    "coreclr.dll",
    "CoreConsole.exe",
    "coredistools.dll",
    "CoreRun.exe",
    "CoreShim.dll",
    "createdump.exe",
    "crossgen.exe",
    "dbgshim.dll",
    "ilasm.exe",
    "ildasm.exe",
    "jitinterface_x64.dll",
    "linuxnonjit.dll",
    "mcs.exe",
    "mscordaccore.dll",
    "mscordbi.dll",
    "mscorrc.dll",
    "protononjit.dll",
    "superpmi.exe",
    "superpmi-shim-collector.dll",
    "superpmi-shim-counter.dll",
    "superpmi-shim-simple.dll",
]


def setup_args(args):
    """ Setup the args for SuperPMI to use.

    Args:
        args (ArgParse): args parsed by arg parser

    Returns:
        args (CoreclrArguments)

    """
    coreclr_args = CoreclrArguments(args, require_built_core_root=False, require_built_product_dir=False,
                                    require_built_test_dir=False, default_build_type="Checked")

    coreclr_args.verify(args,
                        "source_directory",
                        lambda source_directory: os.path.isdir(source_directory),
                        "source_directory doesn't exist")

    coreclr_args.verify(args,
                        "core_root_directory",
                        lambda core_root_directory: os.path.isdir(core_root_directory),
                        "core_root_directory doesn't exist")

    coreclr_args.verify(args,
                        "arch",
                        lambda unused: True,
                        "Unable to set arch")

    coreclr_args.verify(args,
                        "mch_file_tag",
                        lambda unused: True,
                        "Unable to set mch_file_tag")

    coreclr_args.verify(args,
                        "collection_name",
                        lambda unused: True,
                        "Unable to set collection_name")

    coreclr_args.verify(args,
                        "input_directory",
                        lambda input_directory: os.path.isdir(input_directory),
                        "input_directory doesn't exist")

    coreclr_args.verify(args,
                        "max_size",
                        lambda max_size: max_size > 0,
                        "Please enter valid positive numeric max_size",
                        modify_arg=lambda max_size: int(
                            max_size) * 1000 * 1000 if max_size is not None and max_size.isnumeric() else 0
                        # Convert to MB
                        )
    return coreclr_args


def get_files_sorted_by_size(src_directory, exclude_directories, exclude_files):
    """ For a given src_directory, returns all the .dll files sorted by size.

    Args:
        src_directory (string): Path of directory to enumerate.
        exclude_directories ([string]): Directory names to exclude.
        exclude_files ([string]): File names to exclude.
    """

    def sorter_by_size(pair):
        """ Sorts the pair (file_name, file_size) tuple in descending order of file_size

        Args:
            pair ([(string, int)]): List of tuple of file_name, file_size
        """
        pair.sort(key=lambda x: x[1], reverse=True)
        return pair

    filename_with_size = []

    for file_path, dirs, files in walk(src_directory, topdown=True):
        # Credit: https://stackoverflow.com/a/19859907
        dirs[:] = [d for d in dirs if d not in exclude_directories]
        for name in files:
            if name in exclude_files:
                continue
            curr_file_path = path.join(file_path, name)

            if not isfile(curr_file_path):
                continue
            if not name.endswith(".dll") and not name.endswith(".exe"):
                continue

            size = getsize(curr_file_path)
            filename_with_size.append((curr_file_path, size))

    return sorter_by_size(filename_with_size)


def first_fit(sorted_by_size, max_size):
    """ Given a list of file names along with size in descending order, divides the files
    in number of buckets such that each bucket doesn't exceed max_size. Since this is a first-fit
    approach, it doesn't guarantee to find the bucket with tighest spot available.

    Args:
        sorted_by_size ((string, int)): (file_name, file_size) tuple
        max_size (int): Maximum size (in bytes) of each bucket.

    Returns:
        [{int, [string]}]: Returns a dictionary of partition-index to list of file names following in that bucket.
    """
    partitions = {}
    for curr_file in sorted_by_size:
        _, file_size = curr_file

        # Find the right bucket
        found_bucket = False

        if file_size < max_size:
            for p_index in partitions:
                total_in_curr_par = sum(n for _, n in partitions[p_index])
                if (total_in_curr_par + file_size) < max_size:
                    partitions[p_index].append(curr_file)
                    found_bucket = True
                    break

            if not found_bucket:
                partitions[len(partitions)] = [curr_file]

    total_size = 0
    for p_index in partitions:
        partition_size = sum(n for _, n in partitions[p_index])
        print("Partition {0}: {1} bytes.".format(p_index, partition_size))
        total_size += partition_size
    print("Total {0} partitions with {1} bytes.".format(str(len(partitions)), total_size))

    return partitions


def run_command(command_to_run, _cwd=None):
    """ Runs the command.

    Args:
        command_to_run ([string]): Command to run along with arguments.
        _cmd (string): Current working directory.
    """
    print("Running: " + " ".join(command_to_run))
    with subprocess.Popen(command_to_run, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=_cwd) as proc:
        stdout, stderr = proc.communicate()
        if len(stdout) > 0:
            print(stdout.decode("utf-8"))
        if len(stderr) > 0:
            print(stderr.decode("utf-8"))


def copy_directory(src_path, dst_path):
    """Copies directory in 'src_path' to 'dst_path' maintaining the directory
    structure. https://docs.python.org/3.5/library/shutil.html#shutil.copytree can't
    be used in this case because it expects the destination directory should not
    exist, however we do call copy_directory() to copy files to same destination directory.

    It only copied *.dll, *.exe and *.py files.

    Args:
        src_path (string): Path of source directory that need to be copied.
        dst_path (string): Path where directory should be copied.
    """
    if not os.path.exists(dst_path):
        os.makedirs(dst_path)
    for item in os.listdir(src_path):
        src_item = os.path.join(src_path, item)
        dst_item = os.path.join(dst_path, item)
        if os.path.isdir(src_item):
            copy_directory(src_item, dst_item)
        else:
            should_copy_file = dst_item.endswith('.dll') or dst_item.endswith('.py')
            if is_windows:
                should_copy_file = should_copy_file or dst_item.endswith('.exe')
            else:
                should_copy_file = should_copy_file or dst_item.endswith('.so') or item.find(".") == -1
            if not should_copy_file:
                continue
            shutil.copy2(src_item, dst_item)


def copy_files(src_path, dst_path, file_names):
    """Copy files from 'file_names' list from 'src_path' to 'dst_path'.
    It retains the original directory structure of src_path.

    Args:
        src_path (string): Source directory from where files are copied.
        dst_path (string): Destination directory where files to be copied.
        file_names ([string]): List of full path file names to be copied.
    """

    print('### Copying below files to {0}:'.format(dst_path))
    print('')
    print(file_names)
    for f in file_names:
        # Create same structure in dst so we don't clobber same files names present in different directories
        dst_path_of_file = f.replace(src_path, dst_path)

        dst_directory = path.dirname(dst_path_of_file)
        if not os.path.exists(dst_directory):
            os.makedirs(dst_directory)
        shutil.copy2(f, dst_path_of_file)


def partition_files(src_directory, dst_directory, max_size, exclude_directories=[], exclude_files=native_binaries_to_ignore):
    """ Copy bucketized files based on size to destination folder.

    Args:
        src_directory (string): Source folder containing files to be copied.
        dst_directory (string): Destination folder where files should be copied.
        max_size (int): Maximum partition size in bytes
        exclude_directories ([string]): List of folder names to be excluded.
        exclude_files ([string]): List of files names to be excluded.
    """

    sorted_by_size = get_files_sorted_by_size(src_directory, exclude_directories, exclude_files)
    partitions = first_fit(sorted_by_size, max_size)

    index = 0
    for p_index in partitions:
        file_names = [curr_file[0] for curr_file in partitions[p_index]]
        curr_dst_path = path.join(dst_directory, str(index), "binaries")
        copy_files(src_directory, curr_dst_path, file_names)
        index += 1


def set_pipeline_variable(name, value):
    """ This method sets pipeline variable.

    Args:
        name (string): Name of the variable.
        value (string): Value of the variable.
    """
    define_variable_format = "##vso[task.setvariable variable={0}]{1}"
    print("{0} -> {1}".format(name, value)) # logging
    print(define_variable_format.format(name, value)) # set variable


def main(main_args):
    """ Main entrypoint

    Args:
        main_args ([type]): Arguments to the script
    """
    coreclr_args = setup_args(main_args)
    source_directory = coreclr_args.source_directory

    # CorrelationPayload directories
    correlation_payload_directory = path.join(coreclr_args.source_directory, "payload")
    superpmi_src_directory = path.join(source_directory, 'src', 'coreclr', 'scripts')
    superpmi_dst_directory = path.join(correlation_payload_directory, "superpmi")
    arch = coreclr_args.arch
    helix_source_prefix = "official"
    creator = ""
    ci = True
    if is_windows:
        helix_queue = "Windows.10.Arm64" if arch == "arm64" else "Windows.10.Amd64"
    else:
        if arch == "arm":
            helix_queue = "(Ubuntu.1804.Arm32)Ubuntu.1804.Armarch@mcr.microsoft.com/dotnet-buildtools/prereqs:ubuntu-18.04-helix-arm32v7-bfcd90a-20200121150440"
        elif arch == "arm64":
            helix_queue = "(Ubuntu.1804.Arm64)Ubuntu.1804.ArmArch@mcr.microsoft.com/dotnet-buildtools/prereqs:ubuntu-18.04-helix-arm64v8-a45aeeb-20190620155855"
        else:
            helix_queue = "Ubuntu.1804.Amd64"

    # create superpmi directory
    print('Copying {} -> {}'.format(superpmi_src_directory, superpmi_dst_directory))
    copy_directory(superpmi_src_directory, superpmi_dst_directory)
    print('Copying {} -> {}'.format(coreclr_args.core_root_directory, superpmi_dst_directory))
    copy_directory(coreclr_args.core_root_directory, superpmi_dst_directory)

    # Clone and build jitutils
    try:
        with tempfile.TemporaryDirectory() as jitutils_directory:
            run_command(
                ["git", "clone", "--quiet", "--depth", "1", "https://github.com/dotnet/jitutils", jitutils_directory])
            # Set dotnet path to run bootstrap
            os.environ["PATH"] = path.join(source_directory, ".dotnet") + os.pathsep + os.environ["PATH"]
            bootstrap_file = "bootstrap.cmd" if is_windows else "bootstrap.sh"
            run_command([path.join(jitutils_directory, bootstrap_file)], jitutils_directory)

            copy_files(path.join(jitutils_directory, "bin"), superpmi_dst_directory, [path.join(jitutils_directory, "bin", "pmi.dll")])
    except PermissionError as pe_error:
        # Details: https://bugs.python.org/issue26660
        print('Ignoring PermissionError: {0}'.format(pe_error))

    # Workitem directories
    workitem_directory = path.join(source_directory, "workitem")
    pmiassemblies_directory = path.join(workitem_directory, "pmiAssembliesDirectory")

    # payload
    input_artifacts = path.join(pmiassemblies_directory, coreclr_args.collection_name)
    partition_files(coreclr_args.input_directory, input_artifacts, coreclr_args.max_size)

    # Set variables
    print('Setting pipeline variables:')
    set_pipeline_variable("CorrelationPayloadDirectory", correlation_payload_directory)
    set_pipeline_variable("WorkItemDirectory", workitem_directory)
    set_pipeline_variable("InputArtifacts", input_artifacts)
    if is_windows:
        set_pipeline_variable("Python", "py -3")
    else:
        set_pipeline_variable("Python", "python3")
    set_pipeline_variable("Architecture", arch)
    set_pipeline_variable("Creator", creator)
    set_pipeline_variable("Queue", helix_queue)
    set_pipeline_variable("HelixSourcePrefix", helix_source_prefix)
    set_pipeline_variable("MchFileTag", coreclr_args.mch_file_tag)


################################################################################
# __main__
################################################################################

if __name__ == "__main__":
    args = parser.parse_args()
    sys.exit(main(args))
