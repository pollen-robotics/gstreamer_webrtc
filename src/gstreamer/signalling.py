import logging
import time

from gst_signalling import utils


def get_producer_id(host: str, port: int, producer_name: str, timeout: int = 1000) -> str:
    i = 0

    while i < timeout:
        # ToDo: create a client at each iteration. May be not optimal
        producers = utils.get_producer_list(host=host, port=port)

        if producers:
            logging.info("List received, producers:")
            for producer_id, producer_meta in producers.items():
                logging.info(f"  - {producer_id}: {producer_meta}")
                if producer_meta["name"] == producer_name:
                    logging.info("Target producer found.")
                    return str(producer_id)
            logging.warning("Target producer not found.")
        else:
            logging.info("List received, no producers.")

        time.sleep(1)
        i += 1

    return str("")
