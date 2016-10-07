import uuid
import json
import collections

import gevent

import rlp

from web3.utils.string import force_text
from web3.utils.encoding import (
    to_decimal,
    encode_hex,
    decode_hex,
)
from web3.utils.transactions import (
    Transaction,
    serialize_transaction,
    add_signature_to_transaction,
)


class RequestManager(object):
    def __init__(self, provider):
        self.pending_requests = {}
        self.provider = provider

    def setProvider(self, provider):
        self.provider = provider

    def request_blocking(self, method, params):
        """
        Make a synchronous request using the provider
        """
        response_raw = self.provider.make_request(method, params)

        response = json.loads(force_text(response_raw))

        if "error" in response:
            raise ValueError(response["error"])

        return response['result']

    def request_async(self, method, params):
        request_id = uuid.uuid4()
        self.pending_requests[request_id] = gevent.spawn(
            self.request_blocking,
            method,
            params,
        )
        return request_id

    def receive_blocking(self, request_id, timeout=None):
        try:
            request = self.pending_requests.pop(request_id)
        except KeyError:
            raise KeyError("Request for id:{0} not found".format(request_id))
        else:
            if timeout is not None:
                timeout = gevent.Timeout(timeout).start()
            response_raw = request.get(timeout=timeout)

        response = json.loads(response_raw)

        if "error" in response:
            raise ValueError(response["error"])

        return response['result']

    def receive_async(self, request_id, *args, **kwargs):
        raise NotImplementedError("Callback pattern not implemented")


class ManagerWrapper(object):
    def __init__(self, *args, **kwargs):
        self.wrapped_manager = kwargs.pop('wrapped_manager')
        super(ManagerWrapper, self).__init__(*args, **kwargs)

    @property
    def provider(self):
        return self.wrapped_manager.provider

    @property
    def pending_requests(self):
        return self.wrapped_manager.pending_requests

    def setProvider(self, provider):
        self.wrapped_manager.provider = provider

    def request_blocking(self, *args, **kwargs):
        return self.wrapped_manager.request_blocking(*args, **kwargs)

    def request_async(self, *args, **kwargs):
        return self.wrapped_manager.request_async(*args, **kwargs)

    def receive_blocking(self, *args, **kwargs):
        return self.wrapped_manager.receive_blocking(*args, **kwargs)

    def receive_async(self, *args, **kwargs):
        return self.wrapped_manager.receive_async(*args, **kwargs)


class BaseSendRawTransactionMixin(ManagerWrapper):
    _known_transactions = None

    def __init__(self, *args, **kwargs):
        self._known_transactions = collections.defaultdict(set)
        super(BaseSendRawTransactionMixin, self).__init__(*args, **kwargs)

    def _get_nonces_and_cleanup(self, addr, chain_nonce):
        # get a copy of all the currenly known txns
        all_known_txn_hashes = tuple(self._known_transactions[addr])
        for txn_hash in all_known_txn_hashes:
            txn_nonce = to_decimal(self.request_blocking(
                'eth_getTransactionByHash',
                [txn_hash],
            )['nonce'])
            if txn_nonce <= chain_nonce:
                self._known_transactions[addr].discard(txn_hash)
            else:
                yield txn_nonce

    def get_chain_nonce(self, addr):
        chain_nonce = to_decimal(self.request_blocking(
            'eth_getTransactionCount',
            [addr, 'pending']
        ))
        return chain_nonce

    def get_nonce(self, addr):
        chain_nonce = self.get_chain_nonce(addr)
        tracked_txn_nonces = self._get_nonces_and_cleanup(addr, chain_nonce)
        return max(0, chain_nonce, *tracked_txn_nonces)

    def get_transaction_signature(self, serialized_txn):
        raise NotImplementedError("Must be implemented by subclasses")

    def sign_and_serialize_transaction(self, transaction):
        serialized_txn = serialize_transaction(transaction)
        signature = self.get_transaction_signature(transaction)
        signed_transaction = add_signature_to_transaction(
            serialized_txn,
            signature,
        )
        signed_and_serialized_txn = rlp.encode(signed_transaction, Transaction)
        return signed_and_serialized_txn

    def construct_full_transaction(self, base_transaction):
        txn_from = base_transaction['from']
        full_txn = dict(**base_transaction)
        full_txn.setdefault('nonce', self.get_nonce(txn_from))
        full_txn.setdefault('gasPrice', self.request_blocking(
            'eth_gasPrice', []
        ))
        full_txn.setdefault('gas', hex(90000))
        full_txn.setdefault('value', '0x0')
        full_txn.setdefault('to', '')
        full_txn.setdefault('data', '')
        return full_txn

    TXN_SENDING_METHODS = {
        'eth_sendTransaction',
        'eth_sendRawTransaction',
        'personal_signAndSendTransaction',
        'personal_sendTransaction',
    }

    def request_blocking(self, method, params):
        if method == 'eth_sendTransaction':
            base_transaction = params[0]
            # create a fully signed transaction and send through the
            # `eth_sendRawTransaction` endpoint instead.
            full_transaction = self.construct_full_transaction(base_transaction)
            raw_transaction_bytes = self.sign_and_serialize_transaction(
                full_transaction,
            )
            raw_transaction_bytes_as_hex = encode_hex(raw_transaction_bytes)
            return self.request_blocking(
                'eth_sendRawTransaction', [raw_transaction_bytes_as_hex],
            )

        result = super(BaseSendRawTransactionMixin, self).request_blocking(
            method, params,
        )
        if method in self.TXN_SENDING_METHODS:
            txn = self.request_blocking(
                'eth_getTransactionByHash',
                [result],
            )
            txn_from = txn['from']
            self._known_transactions[txn_from].add(result)
        return result


class DelegatedSigningManager(BaseSendRawTransactionMixin):
    def __init__(self, *args, **kwargs):
        self.signature_manager = kwargs.pop('signature_manager')
        super(DelegatedSigningManager, self).__init__(*args, **kwargs)

    def get_chain_nonce(self, addr):
        signer_nonce = to_decimal(self.signature_manager.request_blocking(
            'eth_getTransactionCount',
            [addr, 'pending']
        ))
        wrapped_nonce = to_decimal(self.wrapped_manager.request_blocking(
            'eth_getTransactionCount',
            [addr, 'pending']
        ))
        return max(signer_nonce, wrapped_nonce)

    def get_transaction_signature(self, transaction):
        serialized_txn = serialize_transaction(transaction)
        hash_to_sign = self.signature_manager.request_blocking(
            'web3_sha3', [encode_hex(serialized_txn)],
        )
        signature_hex = self.signature_manager.request_blocking(
            'eth_sign',
            [
                transaction['from'],
                hash_to_sign,
            ],
        )
        signature = decode_hex(signature_hex)
        return signature
