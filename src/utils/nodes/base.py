from abc import ABC, abstractmethod
from typing import Any, Dict, List
import logging

logger = logging.getLogger(__name__)

class PipelineNode(ABC):
    """
    Abstract base class for a processing node.
    """
    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        pass

    def __or__(self, other: 'PipelineNode') -> 'PipelineChain':
        return PipelineChain(self, other)

    def get_nodes(self) -> List['PipelineNode']:
        """Returns itself as a list to help flattening the chain."""
        return [self]


class PipelineChain(PipelineNode):
    """
    A composite node that executes multiple nodes sequentially.
    """
    def __init__(self, first: PipelineNode, second: PipelineNode):
        super().__init__(f"Chain")
        self.first = first
        self.second = second

    def get_nodes(self) -> List[PipelineNode]:
        """Flattens the entire chain into a single linear list of nodes."""
        return self.first.get_nodes() + self.second.get_nodes()

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        nodes = self.get_nodes()
        total_steps = len(nodes)
        
        # El orquestador (la cadena) se encarga de imprimir el paso actual
        for i, node in enumerate(nodes, 1):
            logger.info(f"\n---> [Step {i}/{total_steps}] Running: {node.name} <---")
            
            context = node.run(context)
            
            if "error" in context:
                logger.error(f"Pipeline halted at '{node.name}': {context['error']}")
                break
                
        return context