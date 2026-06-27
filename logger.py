import logging
from rich.logging import RichHandler

# Success log level
SUCCESS = 25
logging.addLevelName(SUCCESS, "SUCCESS")

def success(self, message, *args, **kws):
    if self.isEnabledFor(SUCCESS):
        self._log(SUCCESS, message, args, **kws)
        
logging.Logger.success = success


# Console formatter
class ConsoleFormatter(logging.Formatter):

    #grey = "\x1b[38;20m"
    grey = "\x1b[37m"
    yellow = "\x1b[33;20m"
    red = "\x1b[31;20m"
    cyan = "\x1b[36m"
    green = "\x1b[32m"
    bold_red = "\x1b[31;1m"
    
    reset = "\x1b[0m"
    format="[%(name)s] - %(message)s"
    datefmt="%Y.%m.%d-%H:%M:%S"
    

    FORMATS = {
        logging.DEBUG: grey + format + reset,
        logging.INFO: grey + format + reset,
        logging.WARNING: yellow + format + reset,
        logging.ERROR: red + format + reset,
        logging.CRITICAL: bold_red + format + reset,
        SUCCESS: green + format + reset
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        
        return formatter.format(record)
    
import datetime
from rich.console import Console
from rich.theme import Theme
from rich.logging import RichHandler
from rich.text import Text
import copy
from config import DEBUG


console = Console()


LEVEL_COLORS = {
    "DEBUG":    "grey",
    "INFO":     "bold blue",
    "WARNING":  "yellow",
    "ERROR":    "red",
    "CRITICAL": "bold white on red",
}
LEVEL_TEXT_COLORS = {
    "DEBUG":    "grey",
    "INFO":     "grey",
    "WARNING":  "yellow",
    "ERROR":    "red",
    "CRITICAL": "bold white on red",
}

# Custom Rich Hadler for console output
class CustomRichHandler(RichHandler):
    def __init__(self):
        super().__init__(level = logging.INFO, 
                         rich_tracebacks=DEBUG, 
                         console=console,
                         markup=True,
                         )
        self.setFormatter(logging.Formatter("%(message)s")) 
        
        
    def get_level_text(self, record):
        level = record.levelname
        
        color = LEVEL_COLORS.get(level, "white")
        
        return Text(f" {level:<8}", style=color)
    
    def emit(self, record):
        record = copy.copy(record)
        color = LEVEL_TEXT_COLORS.get(record.levelname, "white")
        record.msg = f"[{color}]{record.getMessage()}[/]"
        record.args = ()
        return super().emit(record)
    


consoleHandler = CustomRichHandler()

# console filter
class ConsoleFilter(logging.Filter):
    def filter(self, record):
        # Если в extra передан file_only=True — блокируем этот handler
        return not getattr(record, "file_only", False)
    
consoleHandler.addFilter(ConsoleFilter())
    
    
# file handler
log_filename = f"logs/latest.log"
fileHandler = logging.FileHandler(log_filename ,mode='w', encoding="utf-8")
fileHandler.setFormatter(logging.Formatter(
    fmt="[%(asctime)s:%(msecs)03d] [%(levelname)s]: - %(name)s - %(message)s (%(filename)s:%(lineno)d)",
    datefmt="%Y-%m-%d %H:%M:%S"
))


# logger body
logging.basicConfig(
                    level=logging.DEBUG, 
                    handlers=[
                        consoleHandler,
                        fileHandler
                    ],
                    force=True
                    )
logger = logging.getLogger('Parser')


import sys
import traceback

def HandleException(e: Exception):
    logger.error(f'{e} at line {traceback.extract_tb(sys.exc_info()[2])[-1][1]}', extra={'file_only': DEBUG})

    
def HandleCritical(e: Exception):
    logger.critical(f'{e} at line {traceback.extract_tb(sys.exc_info()[2])[-1][1]}', extra={'file_only': DEBUG})