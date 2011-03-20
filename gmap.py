#!/usr/bin/env python

#    This file is part of the Minecraft Overviewer.
#
#    Minecraft Overviewer is free software: you can redistribute it and/or
#    modify it under the terms of the GNU General Public License as published
#    by the Free Software Foundation, either version 3 of the License, or (at
#    your option) any later version.
#
#    Minecraft Overviewer is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
#    Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with the Overviewer.  If not, see <http://www.gnu.org/licenses/>.

import sys
if not (sys.version_info[0] == 2 and sys.version_info[1] >= 6):
    print "Sorry, the Overviewer requires at least Python 2.6 to run"  # Python3.0 is not supported either
    sys.exit(1)

import os
import os.path
from configParser import ConfigOptionParser
import re
import subprocess
import multiprocessing
import time
import logging

logging.basicConfig(level=logging.INFO,format="%(asctime)s [%(levelname)s] %(message)s")

# make sure the c_overviewer extension is available
try:
    import c_overviewer
except ImportError:
    print "You need to compile the c_overviewer module to run Minecraft Overviewer."
    print "Run `python setup.py build`, or see the README for details."
    sys.exit(1)

import optimizeimages
import composite
import world
import quadtree

helptext = """
%prog [OPTIONS] <World # / Name / Path to World> <tiles dest dir>
%prog -d <World # / Name / Path to World / Path to cache dir> [tiles dest dir]"""

def main():
    try:
        cpus = multiprocessing.cpu_count()
    except NotImplementedError:
        cpus = 1

    parser = ConfigOptionParser(usage=helptext, config="settings.py")
    parser.add_option("-p", "--processes", dest="procs", help="How many worker processes to start. Default %s" % cpus, default=cpus, action="store", type="int")
    parser.add_option("-z", "--zoom", dest="zoom", help="Sets the zoom level manually instead of calculating it. This can be useful if you have outlier chunks that make your world too big. This value will make the highest zoom level contain (2**ZOOM)^2 tiles", action="store", type="int", configFileOnly=True)
    parser.add_option("-d", "--delete", dest="delete", help="Clear all caches. Next time you render your world, it will have to start completely over again. This is probably not a good idea for large worlds. Use this if you change texture packs and want to re-render everything.", action="store_true", commandLineOnly=True)
    parser.add_option("--cachedir", dest="cachedir", help="Sets the directory where the Overviewer will save chunk images, which is an intermediate step before the tiles are generated. You must use the same directory each time to gain any benefit from the cache. If not set, this defaults to your world directory.")
    parser.add_option("--chunklist", dest="chunklist", help="A file containing, on each line, a path to a chunkfile to update. Instead of scanning the world directory for chunks, it will just use this list. Normal caching rules still apply.")
    parser.add_option("--rendermode", dest="rendermode", help="Specifies the render type: normal (default), lighting, night, or spawn.", type="choice", choices=["normal", "lighting", "night", "spawn"], required=True, default="normal")
    parser.add_option("--imgformat", dest="imgformat", help="The image output format to use. Currently supported: png(default), jpg. NOTE: png will always be used as the intermediate image format.", configFileOnly=True )
    parser.add_option("--optimize-img", dest="optimizeimg", help="If using png, perform image file size optimizations on the output. Specify 1 for pngcrush, 2 for pngcrush+optipng+advdef. This may double (or more) render times, but will produce up to 30% smaller images. NOTE: requires corresponding programs in $PATH or %PATH%", configFileOnly=True)
    parser.add_option("--web-assets-hook", dest="web_assets_hook", help="If provided, run this script after the web assets have been copied, but before actual tile rendering begins. See the README for details.", action="store", metavar="SCRIPT", type="string", configFileOnly=True)
    parser.add_option("-q", "--quiet", dest="quiet", action="count", default=0, help="Print less output. You can specify this option multiple times.")
    parser.add_option("-v", "--verbose", dest="verbose", action="count", default=0, help="Print more output. You can specify this option multiple times.")
    parser.add_option("--skip-js", dest="skipjs", action="store_true", help="Don't output marker.js or regions.js")
    parser.add_option("--display-config", dest="display_config", action="store_true", help="Display the configuration parameters, but don't render the map.  Requires all required options to be specified", commandLineOnly=True)
    #parser.add_option("--write-config", dest="write_config", action="store_true", help="Writes out a sample config file", commandLineOnly=True)

    options, args = parser.parse_args()

    if len(args) < 1:
        print "You need to give me your world number or directory"
        parser.print_help()
        list_worlds()
        sys.exit(1)
    worlddir = args[0]

    if not os.path.exists(worlddir):
        # world given is either world number, or name
        worlds = world.get_worlds()
        
        # if there are no worlds found at all, exit now
        if not worlds:
            parser.print_help()
            print "\nInvalid world path"
            sys.exit(1)
        
        try:
            worldnum = int(worlddir)
            worlddir = worlds[worldnum]['path']
        except ValueError:
            # it wasn't a number or path, try using it as a name
            try:
                worlddir = worlds[worlddir]['path']
            except KeyError:
                # it's not a number, name, or path
                parser.print_help()
                print "Invalid world name or path"
                sys.exit(1)
        except KeyError:
            # it was an invalid number
            parser.print_help()
            print "Invalid world number"
            sys.exit(1)

    if len(args) != 2:
        if options.delete:
            return delete_all(worlddir, None)
        parser.error("Where do you want to save the tiles?")

    destdir = args[1]
    if options.display_config:
        # just display the config file and exit
        parser.display_config()
        sys.exit(0)


    if options.delete:
        return delete_all(worlddir, destdir)

    if options.chunklist:
        chunklist = open(options.chunklist, 'r')
    else:
        chunklist = None

    if options.imgformat:
        if options.imgformat not in ('jpg','png'):
            parser.error("Unknown imgformat!")
        else:
            imgformat = options.imgformat
    else:
        imgformat = 'png'

    if options.optimizeimg:
        optimizeimg = int(options.optimizeimg)
        optimizeimages.check_programs(optimizeimg)
    else:
        optimizeimg = None
    
    if options.web_assets_hook:
        if not os.path.exists(options.web_assets_hook):
            parser.error("Provided hook script does not exist!")
    def web_assets_hook(quadtree):
        if options.web_assets_hook == None:
            return
        try:
            subprocess.check_call((options.web_assets_hook, os.path.abspath(quadtree.destdir)))
        except OSError, e:
            logging.error("could not call web assets hook: %s" % (e,))
            sys.exit(1)
        except subprocess.CalledProcessError:
            logging.error("web assets hook returned error")
            sys.exit(1)
    
    logging.getLogger().setLevel(
        logging.getLogger().level + 10*options.quiet)
    logging.getLogger().setLevel(
        logging.getLogger().level - 10*options.verbose)

    logging.info("Welcome to Minecraft Overviewer!")
    logging.debug("Current log level: {0}".format(logging.getLogger().level))

    if not composite.extension_alpha_over:
        logging.info("Notice: alpha_over extension not found; using default PIL paste()")
    
    useBiomeData = os.path.exists(os.path.join(worlddir, 'biomes'))
    if not useBiomeData:
        logging.info("Notice: Not using biome data for tinting")

    # First do world-level preprocessing
    w = world.World(worlddir, useBiomeData=useBiomeData)
    w.go(options.procs)

    # Now generate the tiles
    # TODO chunklist
    q = quadtree.QuadtreeGen(w, destdir, depth=options.zoom, imgformat=imgformat, optimizeimg=optimizeimg, rendermode=options.rendermode, web_assets_hook=web_assets_hook)
    q.write_html(options.skipjs)
    q.go(options.procs)

def delete_all(worlddir, tiledir):
    # TODO should we delete tiledir here too?
    
    # delete the overviewer.dat persistant data file
    datfile = os.path.join(worlddir,"overviewer.dat")
    if os.path.exists(datfile):
        os.unlink(datfile)
        logging.info("Deleting {0}".format(datfile))

def list_worlds():
    "Prints out a brief summary of saves found in the default directory"
    print 
    worlds = world.get_worlds()
    if not worlds:
        print 'No world saves found in the usual place'
        return
    print "Detected saves:"
    for name, info in sorted(worlds.iteritems()):
        if isinstance(name, basestring) and name.startswith("World") and len(name) == 6:
            try:
                world_n = int(name[-1])
                # we'll catch this one later, when it shows up as an
                # integer key
                continue
            except ValueError:
                pass
        timestamp = time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(info['LastPlayed'] / 1000))
        playtime = info['Time'] / 20
        playstamp = '%d:%02d' % (playtime / 3600, playtime / 60 % 60)
        size = "%.2fMB" % (info['SizeOnDisk'] / 1024. / 1024.)
        print "World %s: %s Playtime: %s Modified: %s" % (name, size, playstamp, timestamp)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
