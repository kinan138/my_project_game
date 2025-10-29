# core/trie.py
from typing import Dict, List, Optional, Any

class TrieNode:
    def __init__(self):
        self.children: Dict[str, "TrieNode"] = {}
        self.is_end_of_word: bool = False
        # נשמור גם טקסט וגם אובייקט, כדי לתמוך בשתי הגרסאות (pygame/web)
        self.word_text: Optional[str] = None
        self.word_object: Optional[Any] = None#אוביט חי שנמצא על המסך כרגע,מילה שנמצאת על המסך כרגע
        self.prefix_count: int = 0

class Trie:
    def __init__(self):
        self.root = TrieNode()

    def insert(self, word_obj: Any):
        """
        מקבל או אובייקט עם .text או מחרוזת רגילה.
        """
        word_text = getattr(word_obj, "text", None)
        if word_text is None:
            word_text = str(word_obj)

        node = self.root
        for ch in word_text.lower():
            if ch not in node.children:
                node.children[ch] = TrieNode()
            node = node.children[ch]
            node.prefix_count += 1
        node.is_end_of_word = True
        node.word_text = word_text
        # נשמור אובייקט רק אם באמת יש .text 
        node.word_object = word_obj if hasattr(word_obj, "text") else None

    def remove(self, word: str):
        word = word.lower()
        path = []
        node = self.root
        for ch in word:
            if ch not in node.children:
                return
            path.append((node, ch))
            node = node.children[ch]
        if not node.is_end_of_word:
            return
        node.is_end_of_word = False
        node.word_text = None
        node.word_object = None
        # עדכון prefix_count וניקוי ענפים ריקים
        for parent, ch in reversed(path):
            child = parent.children[ch]
            child.prefix_count -= 1
            if child.prefix_count <= 0 and not child.is_end_of_word and not child.children:
                del parent.children[ch]
            else:
                break

    def _descend(self, prefix: str) -> Optional[TrieNode]:
        node = self.root
        for ch in prefix.lower():
            if ch not in node.children:
                return None
            node = node.children[ch]
        return node

    def find_words_starting_with(self, prefix: str) -> List[Any]:
        node = self._descend(prefix)
        if not node:
            return []
        out: List[Any] = []
        self._collect(node, out)
        return out

    def _collect(self, node: TrieNode, out: List[Any]):
        if node.is_end_of_word:
            if node.word_object is not None:
                out.append(node.word_object)
            elif node.word_text is not None:
                out.append(node.word_text)
        for child in node.children.values():
            self._collect(child, out)

    def find_best_match(self, typed_prefix: str):
        matches = self.find_words_starting_with(typed_prefix)
        if not matches:
            return None
        try:
            return sorted(
                [m for m in matches if hasattr(m, "position")],
                key=lambda w: w.position[1],
                reverse=True
            )[0]
        except (IndexError, Exception):
            return matches[0]

    def get_prefix_count(self, first_char: str) -> int:
        node = self.root.children.get(first_char.lower())
        return node.prefix_count if node else 0

    def get_all_prefixes(self) -> Dict[str, int]:
        return {ch: node.prefix_count for ch, node in self.root.children.items()}

    def find_urgent_words(self, danger_y: int) -> List[Any]:
        urgent: List[Any] = []
        self._urgent(self.root, urgent, danger_y)
        if urgent:
            urgent.sort(key=lambda w: w.position[1], reverse=True)
        return urgent

    def _urgent(self, node: TrieNode, out: List[Any], danger_y: int):
        if node.is_end_of_word and node.word_object is not None:
            try:
                if node.word_object.position[1] >= danger_y:
                    out.append(node.word_object)
            except Exception:
                pass
        for child in node.children.values():
            self._urgent(child, out, danger_y)


