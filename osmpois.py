import resource
import cProfile
import pstats

import multiprocessing
import os
import shutil
import sys
import time
import argparse
import plyvel
import ujson as json
from imposm.parser import OSMParser
from shapely.geometry import Polygon
import shapely.speedups
from settings import wantedTags, lonelyKeys


def prep_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        'source',
        help='Source file to parse (.pbf, .osm.bz2, or .osm)')
    parser.add_argument(
        '-o',
        '--out',
        help='Destination filename to create (no extension, .geojson gets added on) (default: output)',
        default='output')
    parser.add_argument(
        '--overwrite',
        help='Overwrite any conflicting files.',
        action='store_true')
    parser.add_argument(
        '--remove-lonely',
        help='Remove boring single tag features which might be kept otherwise. See lonelyKeys in settings.py',
        action='store_true')
    parser.add_argument(
        '--require-name',
        help='Only output items that have the \'name\' tag defined.',
        action='store_true')

    return parser

args = vars(prep_args().parse_args())
args['out'] = args['out'] + '.geojson'
print args


def file_prep(db_only=False):
    if not db_only:
        if os.path.isfile(args['out']):
            if args['overwrite']:
                os.remove(args['out'])
            else:
                print 'overwrite conflict with file: ' + args['out']
                print ('remove/rename ' + args['out'] +
                       ', name a different output file with --out, or add the --overwrite option')
                sys.exit()

    if os.path.isdir('coords.ldb'):
        shutil.rmtree('coords.ldb')
    if os.path.isdir('ways.ldb'):
        shutil.rmtree('ways.ldb')


class Ways():
    count = 0
    refs = set()

    def __init__(self, db):
        self.db = db.write_batch()

    def way(self, ways):
        for id, tags, refs in ways:
            if len(tags) and len(refs) > 1 and refs[0] == refs[-1]:
                # circular ways only
                id = str(id)
                tags['OSM_ID'] = 'way/' + id
                self.db.put(id, json.dumps([refs, tags]))
                self.refs.update(refs)
                self.count = self.count + 1

        if self.count > 200000:
            self.batch_write()

    def batch_write(self):
        self.db.write()
        self.count = 0


class Nodes():
    batch = []

    def __init__(self, file):
        self.file = file

    def node(self, nodes):
        for id, tags, coords in nodes:
            if len(tags):
                lat, lon = coords
                lat = "%.5f" % lat
                lon = "%.5f" % lon
                tags['OSM_ID'] = 'node/' + str(id)

                feature = {
                    'type': 'Feature',
                    'geometry': {
                        'type': 'Point',
                        'coordinates': [float(lat), float(lon)]},
                    'properties': tags
                }

                self.batch.append(json.dumps(feature) + '\n,\n')

        if len(self.batch) > 10000:
            self.batch_write()

    def batch_write(self):
        self.file.write(''.join(self.batch))
        self.batch = []


class Coords():
    count = 0
    needed = set()

    def __init__(self, db):
        self.db = db.write_batch()

    def coord(self, coords):
        for id, lat, lon in coords:
            if id in self.needed:
                self.db.put(str(id), str(lat) + ',' + str(lon))
                self.count = self.count + 1

        if self.count > 3000000:
            # ~30MB per million in mem
            self.batch_write()

    def batch_write(self):
        self.db.write()
        self.count = 0


def tag_filter(tags):
    if args['require_name'] and 'name' not in tags:
        for key in tags.keys():
            del tags[key]
            # "functions should modify the dictionary in-place"
    else:
        for key in tags.keys():
            if key not in wantedTags:
                del tags[key]
            else:
                if wantedTags[key] == '*' or tags[key] in wantedTags[key]:
                    a = 1
                    # a = 1 is a placeholder, more to do here, combine keys, normalize values, etc...
                else:
                    del tags[key]

        # remove lonely key
        if args['remove_lonely'] and len(tags) == 1 and tags.keys()[0] in lonelyKeys:
            del tags[tags.keys()[0]]


def include_queue(queue):
    build_POIs.queue = queue


def process(output):
    process.writeDone = False

    queue = multiprocessing.Queue()
    pool = multiprocessing.Pool(None, include_queue, [queue], 1000000)
    go = pool.map_async(build_POIs, waysDB.iterator(), callback=all_done)

    # let the processes start and queues fill up a bit
    time.sleep(1)

    while True:
        if write(output, queue):
            break

    go.wait()
    pool.close()
    pool.join()


def all_done(necessary_arg):
    process.writeDone = True


def build_POIs((id, string)):
    queue = build_POIs.queue

    refs, tags = json.loads(string)
    polygon = build_polygon(refs)

    if polygon and polygon.is_valid:
        tags['POI_AREA'] = polygon.area * 1000000

        if tags['POI_AREA'] > 0.0:
            centroid = polygon.centroid

            feature = {
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': [
                        float("%.5f" % centroid.x),
                        float("%.5f" % centroid.y)]},
                'properties': tags
            }

            queue.put(json.dumps(feature) + '\n,\n')



def build_polygon(refs):
    coords = []

    for ref in refs:
        coord = coordsDB.get(str(ref))
        if coord:
            coord = map(float, coord.split(','))
            coords.append(coord)
        else:
            # for some reason coordinates are missing
            # this is usually because an extract cuts coordinates out
            return False

    if len(coords) > 2:
        # 3 point minimum for polygon
        # avoids common osm problems
        polygon = Polygon(coords)

        if polygon.is_valid:
            return polygon
        else:
            # 0.0 buffer cleans invalid polygons
            # they're invalid for many reasons, people prone problems
            return polygon.buffer(0.0)
    else:
        return False


def write(file, queue):
    # no .qsize on OS X means we use sleep(), lame
    toFile = []

    while not queue.empty():
        toFile.append(queue.get_nowait())

    file.write(''.join(toFile))
    # improvement: batch this

    if process.writeDone:
        return True
    else:
        time.sleep(0.05)
        # might need to be adjusted
        # for some reason the queue is actually quite small
        # documentation says 'infinite', usage says ~60000 chars
            # 300 strings about 200 char long
            # am I just using it wrong?


if __name__ == '__main__':
    prW = cProfile.Profile()
    prW.enable()
    shapely.speedups.enable()

    file_prep()
    waysDB = plyvel.DB('ways.ldb', create_if_missing=True, error_if_exists=True)
    coordsDB = plyvel.DB('coords.ldb', create_if_missing=True, error_if_exists=True)

    output = open(args['out'], 'a')
    output.write('{"type": "FeatureCollection", "features": [\n')

    ways = Ways(waysDB)
    nodes = Nodes(output)
    coords = Coords(coordsDB)

    p = OSMParser(
        ways_callback=ways.way,
        ways_tag_filter=tag_filter,
        nodes_callback=nodes.node,
        nodes_tag_filter=tag_filter)
    print 'parsing ways and passing through nodes'
    p.parse(args['source'])

    ways.batch_write()
    nodes.batch_write()
    coords.needed = ways.refs

    p = OSMParser(coords_callback=coords.coord)
    print 'parsing coordinates'
    p.parse(args['source'])

    coords.batch_write()
    del p, ways, nodes

    print 'processing...'
    process(output)
    file_prep(True)
    output.write(']}')

    print 'saved as: ' + str(args['out'])
    prW.disable()
    print round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 9.53674e-7, 2)

    ps = pstats.Stats(prW)
    ps.sort_stats('time')
    a = ps.print_stats(30)
