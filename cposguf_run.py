import graph_objects as go
import toric_code as tc
import toric_error as te
import unionfind as uf
import unionfind_list as ufl
import unionfind_tree as uft
import multiprocessing as mp
from progiter import ProgIter
import psycopg2 as pgs
from psycopg2 import extras as pgse
from configparser import ConfigParser
import cpuinfo
import time


def read_config(path="./cposguf.ini"):
    cp = ConfigParser()
    cp.read(path)
    sql_config = {}
    comp_id = cp.get("config", "comp_id")
    num_process = cp.getint("config", "num_process")
    iters = cp.getint("config", "iters")
    names = ["host", "port", "database", "user", "password"]
    for name in names:
        sql_config[name] = cp.get("sql_database", name)
    return comp_id, num_process, iters, sql_config


def sql_connection(ini_path=None, get_data=False):

    data = read_config() if ini_path is None else read_config(ini_path)
    con = pgs.connect(**data[3])
    con.set_session(autocommit=True)
    cur = con.cursor()
    if get_data:
        return con, cur, data
    else:
        return con, cur


def add_new_comp(cursor, id, cputype):
    query = (
        "INSERT INTO computers (comp_id, cpu_type) VALUES ('"
        + id
        + "', '"
        + cputype
        + "');"
    )
    cursor.execute(query)


def output_error_array(graph):
    array = [[], [], []]
    for edge in graph.E.values():
        if edge.state:
            array[0].append(edge.qID[1])
            array[1].append(edge.qID[2])
            array[2].append(edge.qID[3])
    return array


def input_error_array(graph, array):
    for y, x, td in list(map(list, zip(*array))):
        graph.E[(0, int(y), int(x), int(td))].state = 1


def fetch_query(selection, L=None, p=None, limit=None, extra=None):
    query = "SELECT {} FROM simulations ".format(selection)
    if L is not None:
        if isinstance(L, int):
            query += "WHERE lattice = " + str(L) + " "
        elif isinstance(L, list):
            query += "WHERE lattice IN " + str(L) + " "
        if any([p is not None, extra is not None]):
            query += "AND "

    if p is not None:
        if L is None:
            query += "WHERE "
        if isinstance(p, float):
            query += "p = " + str(p) + " "
        elif isinstance(p, list):
            query += "p IN " + str(p) + " "
        if extra is not None:
            query += "AND "

    if extra is not None:
        if all([L is None, p is None]):
            query += "WHERE "
        query += extra
        if extra[-1] != " ":
            query += " "

    if limit is not None:
        query += "LIMIT {}".format(limit)

    print("SQL query:", query)
    return query


def multiple(comp_id, iters, size, p, worker=0):
    def single(graph, iter):

        seed = te.init_random_seed(worker=worker, iteration=iter)

        te.init_pauli(graph, pX=p)
        tc.measure_stab(graph)
        uf.find_clusters(graph)
        uft.grow_clusters(graph)
        uf.peel_clusters(graph)
        tc.apply_matching_peeling(graph)
        logical_error = tc.logical_error(graph)
        tree_solved = True if logical_error == [False, False, False, False] else False
        graph.reset()

        te.apply_random_seed(seed)
        te.init_pauli(graph, pX=p)
        tc.measure_stab(graph)
        uf.find_clusters(graph)
        ufl.grow_clusters(graph)
        uf.peel_clusters(graph)
        tc.apply_matching_peeling(graph)
        logical_error = tc.logical_error(graph)
        list_solved = True if logical_error == [False, False, False, False] else False
        graph.reset()

        tree_solved, list_solved

        return (tree_solved, list_solved, seed)

    # Open SQL connection
    con, cur = sql_connection()

    # Simulate
    graph = go.init_toric_graph(size)
    results = [single(graph, iter) for iter in ProgIter(range(iters))]
    diff_res = [result[1:] for result in results if result[0] != result[1]]

    # Insert simulation into database
    query = "INSERT INTO simulations (lattice, p, comp_id, created_on, ftree_tlist, seed) VALUES %s "
    template = "({}, {}, '{}', current_timestamp, %s, %s)".format(
        str(size), str(p), str(comp_id)
    )
    pgse.execute_values(cur, query, diff_res, template)

    # Update cases counters
    cur.execute(
        "SELECT tot_sims, tree_sims, list_sims, tree_wins, list_wins FROM cases WHERE lattice = {} AND p = {}".format(
            size, p
        )
    )
    counter = list(cur.fetchone())
    counter[0] += iters
    for tree_solved, list_solved, _ in results:
        if tree_solved:
            counter[1] += 1
            if not list_solved:
                counter[3] += 1
        if list_solved:
            counter[2] += 1
            if not tree_solved:
                counter[4] += 1

    cur.execute(
        "UPDATE cases SET tot_sims = {}, tree_sims = {}, list_sims = {}, tree_wins = {}, list_wins = {} WHERE lattice = {} and p = {}".format(
            *counter, size, p
        )
    )

    cur.close()
    con.close()


def multiprocess(comp_id, iters, size, p, processes=None):

    if processes is None:
        processes = mp.cpu_count()

    process_iters = iters // processes
    rest_iters = iters - process_iters * processes
    workers = []

    for i in range(processes - 1):
        workers.append(
            mp.Process(target=multiple, args=(comp_id, process_iters, size, p, i))
        )
    workers.append(
        mp.Process(
            target=multiple,
            args=(comp_id, process_iters + rest_iters, size, p, processes - 1),
        )
    )

    for worker in workers:
        worker.start()
    print("Started", processes, "workers.")
    for worker in workers:
        while worker.is_alive():
            time.sleep(2)
            print("Waiting for join...")
        worker.join()


if __name__ == "__main__":

    con, cur, (comp_id, num_process, iters, sql_config) = sql_connection(get_data=1)

    # Check computer listing
    cur.execute("SELECT comp_id FROM computers")
    computers = cur.fetchall()
    if comp_id not in [c[0] for c in computers]:
        cputype = cpuinfo.get_cpu_info()["brand"]
        add_new_comp(cur, comp_id, cputype)

    running = True
    while True:
        # Choose case
        cur.execute("SELECT lattice, p FROM cases_open_free ORDER BY progress LIMIT 1")
        lowest = cur.fetchone()
        if lowest == None:
            cur.execute("SELECT lattice, p FROM cases_open ORDER BY progress LIMIT 1")
            lowest = cur.fetchone()
            if lowest == None:
                exit()
        lattice, deci_p = lowest
        p = float(deci_p)
        print("Selected lattice = {}, p = {}".format(lattice, p))

        # Set computer active case and simulate
        cur.execute(
            "UPDATE computers SET active_lattice = {}, active_p = {} WHERE comp_id = '{}'".format(
                lattice, p, comp_id
            )
        )
        if num_process == 1:
            multiple(comp_id, iters, lattice, p)
        else:
            multiprocess(comp_id, iters, lattice, p, num_process)

        # Check if keep running
        cur.execute(
            "SELECT active_lattice, active_p FROM computers WHERE comp_id = '{}'".format(
                comp_id
            )
        )
        if cur.fetchone() == (None, None):
            print("Stopped by user by database null entry")
            break

    cur.close()
    con.close()
