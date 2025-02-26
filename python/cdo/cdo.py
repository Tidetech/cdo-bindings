import os
import re
import subprocess
import tempfile
import random
import glob
import signal
import threading
import functools
from pkg_resources import parse_version
from io import StringIO
import logging as pyLog
import six
import sys
import threading
import json
try:
    from shutil import which, get_terminal_size
except ImportError:
    from backports.shutil_which import which

# workaround for python2/3 string handling {{{
try:
    from string import strip
except ImportError:
    strip = str.strip
# }}}

# Copyright 2011-2023 Ralf Mueller, ralf.mueller@dkrz.de {{{
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
# 3. Neither the name of the copyright holder nor the names of its contributors
#    may be used to endorse or promote products derived from this software without
#    specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
# }}}

# build interactive documentation: help(cdo.sinfo) {{{

def operator_doc(tool, path2cdo):
    proc = subprocess.Popen('%s -h %s ' % (path2cdo, tool),
                            shell=True,
                            stderr=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    retvals = proc.communicate()
    return retvals[0].decode("utf-8")
# }}}

# return the cdo version {{{

def getCdoVersion(path2cdo, verbose=False):
    proc = subprocess.Popen(
        [path2cdo, '-V'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    retvals = proc.communicate()
    cdo_help = retvals[0].decode("utf-8")
    if verbose:
        return cdo_help
    match = re.search(r"Climate Data Operators version (\d.*) .*", cdo_help)
    return match.group(1)

# helper function without side effects {{{

def setupLogging(logFile):
    logger = pyLog.getLogger(__name__)
    logger.setLevel(pyLog.INFO)

    if isinstance(logFile, six.string_types):
        handler = pyLog.FileHandler(logFile)
    else:
        handler = pyLog.StreamHandler(stream=logFile)

    formatter = pyLog.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger
# }}}

# extra exceptions for CDO {{{

class CDOException(Exception):

    def __init__(self, stdout, stderr, returncode):
        super(CDOException, self).__init__()
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.msg = '(returncode:%s) %s' % (returncode, stderr)

    def __str__(self):
        return self.msg
# }}}

# MAIN Cdo class {{{

class Cdo(object):

    # fallback operator lists {{{
    NoOutputOperators = 'cdiread cmor codetab conv_cmor_table diff diffc diffn \
    diffp diffv dump_cmor_table dumpmap filedes gmtcells gmtxyz gradsdes griddes \
    griddes2 gridverify info infoc infon infop infos infov map ncode ndate \
    ngridpoints ngrids nlevel nmon npar ntime nvar nyear output outputarr \
    outputbounds outputboundscpt outputcenter outputcenter2 outputcentercpt \
    outputext outputf outputfld outputint outputkey outputsrv outputtab outputtri \
    outputts outputvector outputvrml outputxyz pardes partab partab2 seinfo \
    seinfoc seinfon seinfop showattribute showatts showattsglob showattsvar \
    showcode showdate showformat showgrid showlevel showltype showmon showname \
    showparam showstdname showtime showtimestamp showunit showvar showyear sinfo \
    sinfoc sinfon sinfop sinfov spartab specinfo tinfo vardes vct vct2 verifygrid \
    vlist xinfon zaxisdes'.split()
    TwoOutputOperators = 'trend samplegridicon mrotuv eoftime \
    eofspatial eof3dtime eof3dspatial eof3d eof complextorect complextopol'.split()
    MoreOutputOperators = 'distgrid eofcoeff eofcoeff3d intyear scatter splitcode \
    splitday splitgrid splithour splitlevel splitmon splitname splitparam splitrec \
    splitseas splitsel splittabnum splitvar splityear splityearmon splitzaxis'.split()
    AliasOperators = {'seq': 'for'}

    # the following operators introduce additional new lines in cdo-2.0.0 for
    # increased readability in the therminal. This leads to inconsistens parsing
    # behaviour here because before new lines indicated meta data for a new
    # variable for all show* operators.
    ShowTimeOperators = 'showdate showtime showtimestamp showyear showmon'.split()
    # operators are now called with '-s' to ease the parsing process. diff* does
    # not print the errors when '-s' is given, so these operators need special
    # treatment
    # avoiding '-s' can lead to errors when working with operators which write to
    # stdout, but it can done with cdo.silent = False
    DiffOperators = 'diff diffc diffn diffv diffp'.split()
    # }}}

    name = ''

    def __init__(self, #{{{
                 cdo='cdo',
                 returnNoneOnError=False,
                 forceOutput=True,
                 env=os.environ,
                 debug=False,
                 tempdir=tempfile.gettempdir(),
                 tempStore=None,
                 logging=False,
                 logFile=StringIO(),
                 cmd=[],
                 options=[],
                 silent=True):

        if 'CDO' in os.environ and os.path.isfile(os.environ['CDO']):
            self.CDO = os.environ['CDO']
        else:
            self.CDO = cdo

        self._cmd = cmd
        self._options = options

        self.operators = self.__getOperators()
        self.noOutputOperators = [op for op, num in self.operators.items() if 0 == num]
        self.returnNoneOnError = returnNoneOnError
        self.tempStore = tempStore or CdoTempfileStore(dir=tempdir)
        self.forceOutput = forceOutput
        self.env = env
        self.debug = True if 'DEBUG' in os.environ else debug
        self.silent = silent
        self.libs = self.getSupportedLibs()

        # optional IO libraries for additional return types
        self.hasNetcdf = False
        self.hasXarray = False
        self.cdf = None
        self.xa_open = None
        self.__loadOptionalLibs()

        self.logging = logging  # internal logging {{{
        self.logFile = logFile
        if self.logging:
            self.logger = setupLogging(self.logFile)  # }}}

        # CDO build configuration available since cdo-1.9x
        self.config = self.__getConfig()
        #}}}

    def __get__(self, instance, owner):
        if instance is None:
            return self
        name = self.name
        # CDO (version 1.9.6 and older) has an operator called 'for', which cannot
        # called with 'cdo.for()' because 'for' is a keyword in python. 'for' is
        # renamed to 'seq' in 1.9.7.
        # This workaround translates all calls of 'seq' into for in case of
        # versions prior to 1.9.7
        if name in self.AliasOperators and (
                parse_version(getCdoVersion(self.CDO)) < parse_version('1.9.7')):
            name = self.AliasOperators[name]
        return self.__class__(
            instance.CDO,
            instance.returnNoneOnError,
            instance.forceOutput,
            instance.env,
            instance.debug,
            instance.tempStore.dir,
            instance.tempStore,
            instance.logging,
            instance.logFile,
            instance._cmd + ['-' + name],
            instance._options,
            instance.silent)

    # from 1.9.6 onwards CDO returns 1 of diff* finds a difference {{{
    def __exit_success(self, operatorName):
        if parse_version(getCdoVersion(self.CDO)) < parse_version('1.9.6'):
            return 0
        if 'diff' != operatorName[0:4]:
            return 0
        return 1 #}}}

    # read json formatted output of 'cdo --config all' {{{
    def __getConfig(self):
      proc = subprocess.Popen([self.CDO, '--config','all'], stderr=subprocess.PIPE, stdout=subprocess.PIPE)
      ret  = proc.communicate()
      try:
        return json.loads(ret[0].decode('utf-8'))
      except:
        return {} #}}}

    # retrieve the list of operators from the CDO binary plus info out number of {{{
    # output streams
    def __getOperators(self):
        operators = {}

        version = parse_version(getCdoVersion(self.CDO))
        if version < parse_version('1.7.2'):
            proc = subprocess.Popen(
                [self.CDO, '-h'], stderr=subprocess.PIPE, stdout=subprocess.PIPE)
            ret = proc.communicate()
            l = ret[1].decode("utf-8").find("Operators:")
            ops = ret[1].decode("utf-8")[l:-1].split(os.linesep)[1:-1]
            endI = ops.index('')
            s = ' '.join(ops[:endI]).strip()
            s = re.sub(r"\s+", " ", s)

            for op in list(set(s.split(" "))):
                operators[op] = 1
                if op in self.NoOutputOperators:
                    operators[op] = 0
                if op in self.TwoOutputOperators:
                    operators[op] = 2
                if op in self.MoreOutputOperators:
                    operators[op] = -1

        elif version < parse_version('1.8.0') or parse_version('1.9.0') == version:
            proc = subprocess.Popen([self.CDO, '--operators'],
                                    stderr=subprocess.PIPE, stdout=subprocess.PIPE)
            ret = proc.communicate()
            ops = list(map(lambda x: x.split(' ')[0], ret[0].decode(
                "utf-8")[0:-1].split(os.linesep)))

            for op in ops:
                operators[op] = 1
                if op in self.NoOutputOperators:
                    operators[op] = 0
                if op in self.TwoOutputOperators:
                    operators[op] = 2
                if op in self.MoreOutputOperators:
                    operators[op] = -1

        elif version < parse_version('1.9.3'):
            proc = subprocess.Popen([self.CDO, '--operators'],
                                    stderr=subprocess.PIPE, stdout=subprocess.PIPE)
            ret = proc.communicate()
            ops = list(map(lambda x: x.split(' ')[0], ret[0].decode(
                "utf-8")[0:-1].split(os.linesep)))

            proc = subprocess.Popen(
                [self.CDO, '--operators_no_output'],
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE)
            ret = proc.communicate()
            opsNoOutput = list(map(lambda x: x.split(
                ' ')[0], ret[0].decode("utf-8")[0:-1].split(os.linesep)))

            for op in ops:
                operators[op] = 1
                if op in opsNoOutput:
                    operators[op] = 0
                if op in self.TwoOutputOperators:
                    operators[op] = 2
                if op in self.MoreOutputOperators:
                    operators[op] = -1

        else:
            proc = subprocess.Popen([self.CDO, '--operators'],
                                    stderr=subprocess.PIPE, stdout=subprocess.PIPE)
            ret = proc.communicate()
            ops = list(map(lambda x: x.split(' ')[0], ret[0].decode(
                "utf-8")[0:-1].split(os.linesep)))
            ios = list(map(lambda x: x.split(' ')[-1], ret[0].decode(
                "utf-8")[0:-1].split(os.linesep)))

            for i, op in enumerate(ops):
                _ios_split = ios[i][1:len(ios[i]) - 1].split('|')
                if len(_ios_split) > 1:
                    operators[op] = int(_ios_split[1])

        return operators  # }}}

    # execute a single CDO command line {{{
    def __call(self, cmd, envOfCall={}):
        if self.logging and '-h' != cmd[1]:
            self.logger.info(u' '.join(cmd))

        env = dict(self.env)
        env.update(envOfCall)

        proc = subprocess.Popen(' '.join(cmd),
                                shell=True,
                                stderr=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                env=env)

        retvals = proc.communicate()
        stdout = retvals[0].decode("utf-8")
        stderr = retvals[1].decode("utf-8")

        if self.debug:  # debug printing {{{
            print('# DEBUG - start =============================================================')
            # if {} != env:
            #     for k,v in list(env.items()):
            #         print("ENV: " + k + " = " + v)
            print('CALL  :' + ' '.join(cmd))
            print('STDOUT:')
            if 0 != len(stdout.strip()):
                print(stdout)
            print('STDERR:')
            if 0 != len(stderr.strip()):
                print(stderr)
            # }}}
            print('# DEBUG - end ===============================================================')

        return {"stdout": stdout, "stderr": stderr, "returncode": proc.returncode}  # }}}

    # error handling for CDO calls {{{
    def __hasError(self, method_name, cmd, retvals):
        if self.debug:
            print("RETURNCODE:" + retvals["returncode"].__str__())
        if self.__exit_success(method_name) < retvals["returncode"]:
            print("Error in calling operator " + method_name + " with:")
            print(">>> " + ' '.join(cmd) + "<<<")
            print('STDOUT:' + retvals["stdout"])
            print('STDERR:' + retvals["stderr"])

            if self.logging:
                self.logger.error(cmd + " with:" + retvals["stderr"])
            return True
        else:
            return False  # }}}

    # {{{ attempt to load optional libraries: netcdf-IO + XArray
    # numpy is a dependency of both, so no need to check that
    def __loadOptionalLibs(self):
        try:
            import xarray
            self.hasXarray = True
            self.xa_open = xarray.open_dataset
        except Exception:
            print("-->> Could not load xarray!! <<--")

        try:
            from netCDF4 import Dataset as cdf
            import numpy as np
            self.hasNetcdf = True
            self.cdf = cdf
            self.np = np
        except Exception:
            print("-->> Could not load netCDF4! <<--")  # }}}

    def infile(self, *infiles): #{{{
        for infile in infiles:
            if isinstance(infile, six.string_types):
                self._cmd.append(infile)
            elif self.hasXarray:
                import xarray  # <<-- python2 workaround
                if type(infile) == xarray.core.dataset.Dataset:
                    # create a temp nc file from input data
                    tmpfile = self.tempStore.newFile()
                    infile.to_netcdf(tmpfile)
                    self._cmd.append(tmpfile)
        return self #}}}

    def add_option(self, *options): #{{{
        self._options = self._options + list(options)
        return self #}}}

    def __call__(self, *args, **kwargs):
        user_kwargs = kwargs.copy()
        try:
            method_name = self._cmd[0][1:].split(',')[0]
        except IndexError:
            method_name = ''
        operatorPrintsOut = method_name in self.noOutputOperators

        self.envByCall = {}

        # Build the cdo command
        # 0. the cdo command itself
        cmd = [self.CDO]

        # 1. OVERWRITE EXISTING FILES
        cmd.append('-O')

        # 2. set the options
        # show full output in case of diff-like operators
        # or user requested the non-silent mode directly
        if (not method_name in self.DiffOperators) and self.silent:
            cmd.append('-s')
        cmd.extend(self._options)
        # switch to netcdf output in case of numpy/xarray usage
        if kwargs.get('returnArray') is not None \
           or kwargs.get('returnMaArray') is not None \
           or kwargs.get('returnXArray') is not None \
           or kwargs.get('returnXDataset') is not None \
           or kwargs.get('returnCdf') is not None:
            cmd.append('-f nc')
        if 'options' in kwargs:
            cmd += kwargs['options'].split()

        # 3. add operators
        # collect operator parameters and pad them to the operator name
        if len(args) != 0:
            self._cmd[-1] += ',' + ','.join(map(str, args))
        if self._cmd:
            cmd.extend(self._cmd)

        # 4. input files or other operators
        if 'input' in kwargs:
            if isinstance(kwargs["input"], six.string_types):
                cmd.append(kwargs["input"])
            elif type(kwargs["input"]) == list:
                cmd.append(' '.join(kwargs["input"]))
            elif self.hasXarray:
                import xarray  # <<-- python2 workaround
                if type(kwargs["input"]) in [xarray.core.dataset.Dataset,xarray.core.dataarray.DataArray]:
                    # create a temp nc file from input data
                    tmpfile = self.tempStore.newFile()
                    kwargs["input"].to_netcdf(tmpfile)
                    kwargs["input"] = tmpfile

                    cmd.append(kwargs["input"])
            else:
                # we assume it's either a list, a tuple or any iterable.
                cmd.append(kwargs["input"])

        # 5. handle rewrite of existing output files
        if not kwargs.__contains__("force"):
            kwargs["force"] = self.forceOutput

        # 6. handle environment setup per call
        envOfCall = {}
        if kwargs.__contains__("env"):
            for k, v in kwargs["env"].items():
                envOfCall[k] = v

        # 7. output handling: use given outputs or create temporary files
        outputs = []

        # collect the given output
        if kwargs.get("output") is not None:
            outputs.append(kwargs["output"])

        if not user_kwargs or not kwargs.get('compute', True):
            return self
        elif not kwargs.get('keep', True):
            self._cmd.clear()

        if operatorPrintsOut:
            retvals = self.__call(cmd, envOfCall)
            if not self.__hasError(method_name, cmd, retvals):
                r = list(map(strip, retvals["stdout"].split(os.linesep)))
                if "autoSplit" in kwargs:
                    splitString = kwargs["autoSplit"]
                    _output = [x.split(splitString) for x in r[:len(r) - 1]]
                    if 1 == len(_output):
                        return _output[0]
                    else:
                        return _output
                else:
                    return r[:len(r) - 1]
            else:
                if self.returnNoneOnError:
                    return None
                else:
                    raise CDOException(**retvals)
        else:
            if kwargs["force"] or \
               (kwargs.__contains__("output") and not os.path.isfile(kwargs["output"])):
                if not kwargs.__contains__("output") or kwargs["output"] is None:
                    for i in range(0, self.operators[method_name]):
                        outputs.append(self.tempStore.newFile())

                cmd.append(' '.join(outputs))

                retvals = self.__call(cmd, envOfCall)
                if self.__hasError(method_name, cmd, retvals):
                    if self.returnNoneOnError:
                        return None
                    else:
                        raise CDOException(**retvals)
            else:
                if self.debug:
                    print(("Use existing file'" + kwargs["output"] + "'"))

        # defaults for file handles as return values
        if not kwargs.__contains__("returnCdf"):
            kwargs["returnCdf"] = False
        if not kwargs.__contains__("returnXDataset"):
            kwargs["returnXDataset"] = False

        # return data arrays
        if kwargs.get("returnArray") is not None:
            return self.readArray(outputs[0], kwargs["returnArray"])
        elif kwargs.get("returnMaArray") is not None:
            return self.readMaArray(outputs[0], kwargs["returnMaArray"])
        elif kwargs.get("returnXArray") is not None:
            return self.readXArray(outputs[0], kwargs.get("returnXArray"))

        # return files handles (or lists of them)
        elif kwargs["returnCdf"]:
            if 1 == len(outputs):
                return self.readCdf(outputs[0])
            else:
                return [self.readCdf(file) for file in outputs]
        elif kwargs["returnXDataset"]:
            if 1 == len(outputs):
                return self.readXDataset(outputs[0])
            else:
                return [self.readXDataset(file) for file in outputs]

        # handle split-operator outputs
        elif 'split' == method_name[0:5]:
            return glob.glob(kwargs["output"] + '*')

        # default: return filename (given or tempfile)
        else:
            if 1 == len(outputs):
                return outputs[0]
            else:
                return outputs

    def __getattr__(self, method_name):  # main method-call handling for Cdo-objects {{{
        if any(method_name in opts for opts in (
               self.__dict__, self.operators, self.AliasOperators)):
            if self.debug:
                print(("Found operator:" + method_name))

            # cache the method for later
            class Operator(self.__class__):
                name = method_name
                __name__ = method_name
                __qualname__ = getattr(  # __qualname__ is available in python 3.3+
                    self.__class__, '__qualname__', self.__class__.__name__
                ) + '.' + method_name

                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.__doc__ = operator_doc(method_name, self.CDO)

            setattr(self.__class__, method_name, Operator())
            return getattr(self, method_name)
        else:
            # given method might match part of know operators: autocompletion
            func = lambda x: re.search(method_name, x)
            options = list(filter(func, self.operators))
            message = "Unknown operator '" + method_name + "'!"
            if 0 != len(options):
                message += " Did you mean: " + ", ".join(options) + "?"
            raise AttributeError(message)
    # }}}

    # try to find out the possible output formats {{{
    # This can be replaces by the parsing of the --config option output in 2.x releases
    def getSupportedLibs(self):
        proc = subprocess.Popen(self.CDO + ' -V',
                                shell=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        retvals = proc.communicate()
        withs = list(re.findall('(with|Features): (.*)',
                                retvals[0].decode("utf-8"))[0])[1].split(' ')

        # do an additional split if the entry has a /
        # and collect everything into a flatt list
        withs = list(map(lambda x: x.split('/') if re.search(r'\/', x) else x, withs))
        allWiths = []
        for _withs in withs:
            if isinstance(_withs, list):
                for __withs in _withs:
                    allWiths.append(__withs)
            else:
                allWiths.append(_withs)
        withs = allWiths

        libs = re.findall(r'(\w+) library version : (\d+\.\S+) ',
                          retvals[0].decode("utf-8"))
        libraries = dict({})
        for w in withs:
            libraries[w.lower()] = True

        for lib in libs:
            l, v = lib
            libraries[l.lower()] = v

        return libraries #}}}

    def collectLogs(self):
        if isinstance(self.logFile, six.string_types):
            content = []
            with open(self.logFile, 'r') as f:
                content.append(f.read())
            return ''.join(content)
        else:
            self.logFile.flush()
            return self.logFile.getvalue()

    def showLog(self):
        print(self.collectLogs())

    # check if the current (or given) CDO binary works
    def hasCdo(self, path=None):
        if path is None:
            path = self.CDO
        cmd = [path, ' -V', '>/dev/null 2>&1']
        executable = (0 == self.__call(cmd)["returncode"])
        fullpath = (os.path.isfile(path) and os.access(path, os.X_OK))
        return (executable or fullpath)

    # selfcheck for the current CDO binary
    def check(self):
        if not self.hasCdo():
            return False
        if self.debug:
            print(self.__call([self.CDO, ' -V']))
        return True

    # change the CDO binary for the current object
    def setCdo(self, value):
        self.CDO = value
        self.operators = self.__getOperators()

    # return the path to the CDO binary currently used
    def getCdo(self):
        return self.CDO

    def hasLib(self, lib):
        return lib in self.libs

    def libsVersion(self, lib):
        if not self.hasLib(lib):
            raise AttributeError("Cdo does NOT have support for '#{lib}'")
        else:
            if self.libs[lib] is not True:
                return self.libs[lib]
            else:
                print("No version information available about '" + lib + "'")
                return False

    def cleanTempDir(self):
        self.tempStore.cleanTempDir()

    # make use of internal documentation structure of python
    def __dir__(self):
        res = dir(type(self)) + list(self.__dict__)
        res.extend(list(self.operators))
        return res

    # ==================================================================
    # Addional operators:
    # ------------------------------------------------------------------

    def version(self, verbose=False):
        # return CDO's version
        return getCdoVersion(self.CDO, verbose)

    def boundaryLevels(self, **kwargs):
        ilevels = list(map(float, self.showlevel(input=kwargs['input'])[0].split()))
        bound_levels = []
        bound_levels.insert(0, 0)
        for i in range(1, len(ilevels) + 1):
            bound_levels.insert(
                i, bound_levels[i - 1] + 2 * (ilevels[i - 1] - bound_levels[i - 1]))

        return bound_levels

    def thicknessOfLevels(self, **kwargs):
        bound_levels = self.boundaryLevels(**kwargs)
        delta_levels = []
        for i in range(0, len(bound_levels)):
            v = bound_levels[i]
            if 0 == i:
                continue

            delta_levels.append(v - bound_levels[i - 1])

        return delta_levels

    def run(self, output=None):
        if output:
            return self(output=output, compute=True)
        else:
            return self(compute=True)

    def readCdf(self, iFile=None):
        """Return a cdf handle created by the available cdf library"""
        if iFile is None:
            iFile = self.run()
        if self.hasNetcdf:
            fileObj = self.cdf(iFile, mode='r')
            return fileObj
        else:
            print("Could not import data from file '%s' (python-netCDF4)" % iFile)
            six.raise_from(ImportError, None)

    def readArray(self, iFile=None, varname=None):
        """Direcly return a numpy array for a given variable name"""
        if iFile is None:
            iFile = self.run()
        if varname is None:
            raise ValueError("A varname needs to be specified!")
        filehandle = self.readCdf(iFile)
        try:
            # return the data array for given variable name
            return filehandle.variables[varname][:].copy()
        except Exception:
            print("Cannot find variable '%s'" % varname)
            six.raise_from(LookupError, None)

    def readMaArray(self, iFile=None, varname=None):  # {{{
        """Create a masked array based on cdf's FillValue"""
        if iFile is None:
            iFile = self.run()
        if varname is None:
            raise ValueError("A varname needs to be specified!")
        fileObj = self.readCdf(iFile)

        if varname not in fileObj.variables:
            print("Cannot find variables '%s'" % varname)
            six.raise_from(LookupError, None)
        else:
            data = fileObj.variables[varname][:].copy()

        if hasattr(fileObj.variables[varname], '_FillValue'):
            # return masked array
            retval = self.np.ma.array(
                data, mask=data == fileObj.variables[varname]._FillValue)
        else:
            # generate dummy mask which is always valid
            retval = self.np.ma.array(data, mask=data != data)

        return retval  # }}}

    def readXArray(self, ifile=None, varname=None):
        if ifile is None:
            ifile = self.run()
        if varname is None:
            raise ValueError("A varname needs to be specified!")
        if not self.hasXarray:
            print("Could not load XArray")
            six.raise_from(ImportError, None)

        dataSet = self.xa_open(ifile)
        try:
            return dataSet[varname]
        except Exception:
            print("Cannot find variable '%s'" % varname)
            six.raise_from(LookupError, None)

    def readXDataset(self, ifile=None):
        if ifile is None:
            ifile = self.run()
        if not self.hasXarray:
            print("Could not load XArray")
            six.raise_from(ImportError, None)

        return self.xa_open(ifile)

    def __print__(self, context=''):
        if '' != context:
            print('CDO:CONTEXT ' + context)
        print("CDO:ID  = " + str(id(self)))
        print("CDO:ENV = " + str(self.env))
# }}}

# Helper module for easy temp file handling {{{

class CdoTempfileStore(object):

    __tempfiles = []

    def __init__(self, dir):
        self.persistent_tempfile = False
        self.fileTag = 'cdoPy'
        self.dir = dir
        if not os.path.isdir(dir):
            os.makedirs(dir)
        # handling different exits from interactive sessions
        # python3 has threading.main_thread(), but python2 doesn't
        if sys.version_info[0] == 2 \
                or threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGSEGV):
                sig_default = signal.getsignal(sig)
                sig_wrapped = functools.partial(self.__catch__, throw=sig_default)
                signal.signal(sig, sig_wrapped)
                signal.siginterrupt(sig, False)

    def __del__(self):
        # remove temporary files
        for filename in self.__class__.__tempfiles:
            if os.path.isfile(filename):
                os.remove(filename)

    def __catch__(self, signum, frame, throw=None, **kwargs):
        # if a termination signal could be caught, remove tempfile
        self.__del__()
        if callable(throw):
            throw(signum, frame, **kwargs)
        else:
            print("caught signal", signum, frame)

    def cleanTempDir(self):
        leftOvers = [os.path.join(self.dir, f) for f in os.listdir(self.dir)]
        # filter for cdo.py's tempfiles owned by you
        leftOvers = [f for f in leftOvers if
                     self.fileTag in f
                     and os.path.isfile(f)
                     and os.stat(f).st_uid == os.getuid()]
        # this might lead to trouble if it is used by server side computing like
        # jupyter notebooks, filtering by userid might no be enough
        for f in leftOvers:
            os.remove(f)

    def setPersist(self, value):
        self.persistent_tempfiles = value

    def newFile(self):
        if not self.persistent_tempfile:
            t = tempfile.NamedTemporaryFile(
                delete=True, prefix=self.fileTag, dir=self.dir)
            self.__class__.__tempfiles.append(t.name)
            t.close()

            return t.name
        else:
            N = 10000000
            return "_" + random.randint(0, N).__str__()
# }}}

# vim: expandtab tabstop=4 shiftwidth=4 softtabstop=4 fdm=marker
