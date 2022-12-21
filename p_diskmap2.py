from multiprocessing import Queue, Process, Value, Semaphore
import joblib
from io import BytesIO
import time
import os
from tqdm import tqdm
import mmap
import gc

BYTE_SIZE = 8


def generate_tmp_file():
    timestamp = time.time()
    return "/tmp/p_diskcache_%s.dat" % timestamp


def dump_to_bytes(obj):
    f = BytesIO()
    joblib.dump(obj, f)
    f.seek(0)
    byte_buffer = f.read()
    f.close()
    return byte_buffer


def load_from_byte_buffer(byte_buffer):
    return joblib.load(byte_buffer)


def write_xobject_to_bin_file(obj, f, flush=True, bin_obj=False):
    if bin_obj:
        out_bytes = obj
    else:
        out_bytes = dump_to_bytes(obj)
    # Get the size
    sz = len(out_bytes)
    # assert sz <= MAX_SIZE

    # Write the size in binary format
    f.write(int(sz).to_bytes(BYTE_SIZE, "little"))
    # Write the corresponding binary data
    f.write(out_bytes)
    # Flush to disk
    if flush:
        f.flush()


def read_next_xobject_from_bin_file(f, offset=None):
    if offset is not None:
        f.seek(offset)
    sz = f.read(BYTE_SIZE)
    if not sz:
        return None  # loop termination signal in read_all_xobject_from_bin_file
    # Read the corresponding result of that size
    sz = int.from_bytes(sz, "little")
    v = f.read(sz)  # file pointer moves automatically to the end of current pos + sz
    v = read_binary(v)
    return v


def read_xobject_from_file_offset_list(f, offset_list):
    xobject_list = []
    for offset in offset_list:
        xobject_list.append(read_next_xobject_from_bin_file(f, offset=offset))
    return xobject_list


def read_binary(v):
    byte_buffer = BytesIO()
    byte_buffer.write(v)
    v = joblib.load(byte_buffer)
    byte_buffer.close()
    return v


def read_bin_file_offset_list(f, rewind=False):
    offset_list = []
    c_offset = 0

    while True:
        sz = f.read(BYTE_SIZE)
        if not sz:
            break
        else:
            offset_list.append(c_offset)

        sz = int.from_bytes(sz, "little")
        next_offset = sz + BYTE_SIZE + c_offset
        f.seek(sz, 1)
        c_offset = next_offset
    if rewind:
        f.seek(0)
    else:
        f.close()
    return offset_list  ## list of positions of segement starts in file (binary file)


def read_all_xobject_from_bin_file(f):
    ss = []
    while True:
        # Read size of the next result in BYTE_SIZE bytes
        v = read_next_xobject_from_bin_file(f)
        if v is None:
            break
        ss.append(v)
    f.close()
    return ss


def read_xobject_from_bin_file(f):
    ss = []
    while True:
        # Read size of the next result in BYTE_SIZE bytes
        v = read_next_xobject_from_bin_file(f)
        if v is None:
            break
        ss.append(v)
    f.close()
    return ss


def produce(func, idatum, queue, max_q_size=300, to_bin=True, fin_path=None, sub_fc=None, **kwargs):
    if fin_path is not None:
        fin = open(fin_path, "rb")
    else:
        fin = None
    print("Start process: ", os.getpid())

    for idata in idatum:
        while queue.qsize() >= max_q_size - 4:
            time.sleep(1)
            continue
        if fin is not None:
            offset = idata
            i, data = read_next_xobject_from_bin_file(fin, offset)
        else:
            i, data = idata
        re = func(*data, **kwargs)
        if sub_fc is not None:
            sub_re = sub_fc(*re)
            if to_bin:
                d = dump_to_bytes([i, re]), dump_to_bytes([i, sub_re])
            else:
                d = [i, re], [i, sub_re]
        else:
            if to_bin:
                d = dump_to_bytes([i, re])
            else:
                d = [i, re]

        queue.put(d)
        del d
        del re

        # time.sleep(0.1)
    print("End process: ", os.getpid())


def consume(queue, n_completed, n_total, fout=None, desc=None, buffer_max_size=50, fout2=None):
    buffer = []
    pbar = tqdm(total=n_total, desc=desc)

    while True:
        data = queue.get()
        n_completed.value += 1
        # print("Dat: ", data)

        if fout2 is not None:
            byte_data, byte_data2 = data
        else:
            byte_data, byte_data2 = data, None

        buffer.append([byte_data, byte_data2])
        # print("Size: ", queue.qsize())
        if len(buffer) == buffer_max_size:
            # print("Writing to file...")
            for byte_data, byte_data2 in buffer:

                write_xobject_to_bin_file(byte_data, fout, flush=False, bin_obj=True)
                if byte_data2 is not None:
                    write_xobject_to_bin_file(byte_data2, fout2, flush=False, bin_obj=True)
                del byte_data
                del byte_data2
            # print("Flushing...")
            fout.flush()
            if fout2 is not None:
                fout2.flush()
            pbar.update(buffer_max_size)
            buffer.clear()
            gc.collect()

            assert len(buffer) == 0
        if n_completed.value == n_total:
            if len(buffer) != 0:
                for byte_data, byte_data2 in buffer:
                    write_xobject_to_bin_file(byte_data, fout, flush=False, bin_obj=True)
                    if byte_data2 is not None:
                        write_xobject_to_bin_file(byte_data2, fout2, flush=False, bin_obj=True)
                    del byte_data
                    del byte_data2
                pbar.update(len(buffer))
                fout.flush()
                if fout2 is not None:
                    fout2.flush()
                buffer.clear()
                gc.collect()

            break
    pbar.close()


def p_diskmap(func, datum, fout_path=None, fout_path2=None, sub_func=None, desc=None, njob=4, n_buffer_size=10,
              max_queue_size=300, **kwargs):
    n_total = len(datum)
    idatum = []
    for i, data in enumerate(datum):
        idatum.append([i, data])
    job_size = n_total // njob
    producers = []
    queue = Queue()
    n_completed = Value('i', 0)

    for i in range(njob):
        start_id = i * job_size
        end_id = (i + 1) * job_size
        if i == njob - 1:
            end_id = n_total
        producer = Process(target=produce,
                           args=(func, idatum[start_id:end_id], queue, max_queue_size, True, None, sub_func),
                           kwargs=kwargs)
        producers.append(producer)
    if fout_path is None:
        fout_path = generate_tmp_file()

    fout = open(fout_path, "wb")
    fout2 = None
    if sub_func is not None:
        if fout_path2 is None:
            fout_path2 = "%s_2" % fout_path
        fout2 = open(fout_path2, "wb")

    consumer = Process(target=consume, args=(queue, n_completed, n_total, fout, desc, n_buffer_size, fout2))

    for p in producers:
        p.start()
    consumer.start()
    for p in producers:
        p.join()
    consumer.join()
    fout.close()
    if fout2 is not None:
        fout2.close()
    return fout_path, fout_path2


def p_diskmap_from_file(func, fin_path, fout_path=None, desc=None, njob=4, n_buffer_size=10, max_queue_size=300,
                        **kwargs):
    fin = open(fin_path, "rb")
    datum = read_bin_file_offset_list(fin)
    n_total = len(datum)
    print("N_Total: ", n_total)
    idatum = []
    for _, offset in enumerate(datum):
        idatum.append(offset)
    job_size = n_total // njob
    producers = []
    queue = Queue()
    n_completed = Value('i', 0)

    for i in range(njob):
        start_id = i * job_size
        end_id = (i + 1) * job_size
        if i == njob - 1:
            end_id = n_total
        producer = Process(target=produce,
                           args=(func, idatum[start_id:end_id], queue, max_queue_size, True, fin_path, None),
                           kwargs=kwargs)
        producers.append(producer)
    if fout_path is None:
        fout_path = generate_tmp_file()

    fout = open(fout_path, "wb")
    consumer = Process(target=consume, args=(queue, n_completed, n_total, fout, desc, n_buffer_size))

    for p in producers:
        p.start()
    consumer.start()
    for p in producers:
        p.join()
    consumer.join()
    fout.close()
    return fout_path


def fx(a1, a2, vy=0, vz=0):
    return a1 + vy, a2 + vz


def sub_f(v1, v2):
    return v2


def tt():
    datum = [(1 * i, 2 * i) for i in range(1001)]
    out_path, out_path2 = p_diskmap(fx, datum, fout_path=None, desc="Receive ", n_buffer_size=200, max_queue_size=400,
                                    vy=1, vz=1, sub_func=sub_f)
    print(out_path, out_path2)

    # r = read_all_xobject_from_bin_file(open(out_path, "rb"))
   #  print(r)
    r = read_all_xobject_from_bin_file(open(out_path2, "rb"))
    print(r)


if __name__ == "__main__":
    tt()
